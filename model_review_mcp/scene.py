"""Model loading: the 3MF reader and the scene that lays several files out side by side."""
import functools, json, re, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"


MAIN_MODEL = "/3D/3dmodel.model"


def read_matrix(transform: str | None) -> np.ndarray:
    """Return a 4x4 matrix for a 3MF transform attribute; identity when it is missing."""
    m = np.eye(4)
    if transform:  # 3MF stores a row-vector 4x3 matrix: "m00 m01 m02 m10 ... m32"
        m[:3, :] = np.array(transform.split(), dtype=float).reshape(4, 3).T
    return m


def metadata_value(element, key: str) -> str | None:
    """Return the value of the slicer `<metadata key=... value=...>` child with this key."""
    return next((m.get("value") for m in element.findall("metadata") if m.get("key") == key), None)


def read_object_settings(z: zipfile.ZipFile) -> tuple[dict, dict, dict]:
    """Read object names, part names and extruders from the Bambu Studio / OrcaSlicer metadata.

    Returns three dicts keyed by object id: the object name, {part id: part name} and
    {part id: extruder}, where the key None holds the object's own extruder.
    """
    names, parts, extruders = {}, {}, {}
    if "Metadata/model_settings.config" in z.namelist():
        for o in ET.fromstring(z.read("Metadata/model_settings.config")).iter("object"):
            oid = o.get("id")
            names[oid] = metadata_value(o, "name")
            parts[oid] = {pt.get("id"): metadata_value(pt, "name") for pt in o.iter("part")}
            # a part without its own extruder prints with the object's
            extruders[oid] = {pt.get("id"): metadata_value(pt, "extruder") or metadata_value(o, "extruder")
                              for pt in o.iter("part")}
            extruders[oid][None] = metadata_value(o, "extruder")
    return names, parts, extruders


def read_filament_colours(z: zipfile.ZipFile) -> list[str]:
    """Return the filament colours of the slicer project, indexed by extruder number - 1."""
    if "Metadata/project_settings.config" in z.namelist():
        return json.loads(z.read("Metadata/project_settings.config")).get("filament_colour", [])
    return []


def read_mesh(mesh) -> trimesh.Trimesh:
    """Build a mesh from a 3MF `<mesh>` element, keeping vertices and faces as stored."""
    v = np.array([[float(e.get(a)) for a in "xyz"] for e in mesh.iter(CORE + "vertex")])
    f = np.array([[int(e.get(a)) for a in ("v1", "v2", "v3")] for e in mesh.iter(CORE + "triangle")])
    return trimesh.Trimesh(v, f, process=False)


def walk_meshes(model_file, path: str, oid: str, transform: np.ndarray, part_id: str | None):
    """Yield (part id, mesh in plate coordinates) for every mesh below an object, following components.

    `model_file(path)` returns ({object id: <object>}, root) for a model file in the archive. A
    component may point into another model file; its object id becomes the part id.
    """
    o = model_file(path)[0][oid]
    mesh = o.find(CORE + "mesh")
    if mesh is not None:
        yield part_id, read_mesh(mesh).apply_transform(transform)
        return
    for c in o.iter(CORE + "component"):
        yield from walk_meshes(model_file, c.get(PROD + "path") or path, c.get("objectid"),
                               transform @ read_matrix(c.get("transform")), c.get("objectid"))


def part_label(object_name: str, part_name: str, only_part: bool) -> str:
    """Name a part for the viewer: the object name alone for a single part, else object and part."""
    if only_part:
        return object_name
    if part_name.startswith(object_name):  # Bambu often names parts after the object
        return part_name
    return f"{object_name} / {part_name}"


def part_colour(object_extruders: dict, part_id: str | None, colours: list[str]) -> str | None:
    """Return the filament colour of the part's extruder (or the object's), if the project has one."""
    ext = object_extruders.get(part_id) or object_extruders.get(None)
    return colours[int(ext) - 1] if ext and 0 < int(ext) <= len(colours) else None


def load_3mf(path: Path) -> list[tuple[str, trimesh.Trimesh, str | None]]:
    """Return (name, mesh in plate coordinates, filament colour) for every part of every build item.

    A small reader of our own, because trimesh resolves every component that lives in a shared
    object file (the way Bambu Studio stores multi-part objects) to the first mesh in that file.
    Part names and filament colours come from the Bambu Studio / OrcaSlicer metadata when present.
    """
    z = zipfile.ZipFile(path)

    @functools.cache
    def model_file(p):
        root = ET.fromstring(z.read(p.lstrip("/")))
        return {o.get("id"): o for o in root.iter(CORE + "object")}, root

    names, part_names, extruders = read_object_settings(z)
    colours = read_filament_colours(z)
    out = []
    for i, item in enumerate(model_file(MAIN_MODEL)[1].iter(CORE + "item")):
        oid = item.get("objectid")
        found = list(walk_meshes(model_file, MAIN_MODEL, oid, read_matrix(item.get("transform")), None))
        object_name = names.get(oid) or f"Object {i + 1}"
        for part_id, mesh in found:
            part_name = part_names.get(oid, {}).get(part_id) or f"part {part_id}"
            out.append((part_label(object_name, part_name, len(found) == 1), mesh,
                        part_colour(extruders.get(oid, {}), part_id, colours)))
    return out


GAP = 20.0  # mm between files shown side by side


def build_scene(paths: list[str]) -> tuple[bytes, list[str], dict[str, str], dict[str, list[float]]]:
    """Load the files into one scene.

    Returns the GLB, the part names, the known part colours and, per part, the offset that was added
    to lay several files out side by side (slicer projects all sit in the middle of the plate and
    would overlap). The viewer subtracts the offset, so notes keep each file's own coordinates.
    """
    scene, colours, offsets, cursor = trimesh.Scene(), {}, {}, None
    for p in map(Path, paths):
        if p.suffix.lower() == ".3mf":
            meshes = load_3mf(p)
        else:
            loaded = trimesh.load(p)
            meshes = [(p.stem, loaded, None)] if isinstance(loaded, trimesh.Trimesh) else \
                [(f"{p.stem} / {g.metadata.get('name', i)}", g, None) for i, g in enumerate(loaded.dump())]
        lo = np.min([m.bounds[0] for _, m, _ in meshes], axis=0)
        hi = np.max([m.bounds[1] for _, m, _ in meshes], axis=0)
        shift = np.zeros(3) if cursor is None else np.array([cursor - lo[0], 0.0, 0.0])
        cursor = hi[0] + shift[0] + GAP
        for name, mesh, colour in meshes:
            name = re.sub(r"\.(stl|obj|ply|step|stp)$", "", name, flags=re.I)
            if len(paths) > 1 and name != p.stem:
                name = f"{p.stem}: {name}"
            scene.add_geometry(mesh.copy().apply_translation(shift), node_name=name, geom_name=name)
            offsets[name] = shift.round(4).tolist()
            if colour:
                colours[name] = colour[:7]  # "#RRGGBB" (drop a possible alpha byte)
    return scene.export(file_type="glb"), list(scene.graph.nodes_geometry), colours, offsets
