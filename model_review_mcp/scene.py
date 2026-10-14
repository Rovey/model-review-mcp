"""Model loading: the 3MF reader and the scene that lays several files out side by side."""
import json, re, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"


def load_3mf(path: Path) -> list[tuple[str, trimesh.Trimesh, str | None]]:
    """Return (name, mesh in plate coordinates, filament colour) for every part of every build item.

    A small reader of our own, because trimesh resolves every component that lives in a shared
    object file (the way Bambu Studio stores multi-part objects) to the first mesh in that file.
    Part names and filament colours come from the Bambu Studio / OrcaSlicer metadata when present.
    """
    z = zipfile.ZipFile(path)
    docs = {}

    def objects(p):
        if p not in docs:
            root = ET.fromstring(z.read(p.lstrip("/")))
            docs[p] = {o.get("id"): o for o in root.iter(CORE + "object")}, root
        return docs[p][0]

    def matrix(s):
        m = np.eye(4)
        if s:  # 3MF stores a row-vector 4x3 matrix: "m00 m01 m02 m10 ... m32"
            m[:3, :] = np.array(s.split(), dtype=float).reshape(4, 3).T
        return m

    names, parts, extruders, colours = {}, {}, {}, []
    if "Metadata/model_settings.config" in z.namelist():
        meta = lambda e, key: next((m.get("value") for m in e.findall("metadata") if m.get("key") == key), None)
        for o in ET.fromstring(z.read("Metadata/model_settings.config")).iter("object"):
            oid = o.get("id")
            names[oid] = meta(o, "name")
            parts[oid] = {pt.get("id"): meta(pt, "name") for pt in o.iter("part")}
            # a part without its own extruder prints with the object's
            extruders[oid] = {pt.get("id"): meta(pt, "extruder") or meta(o, "extruder") for pt in o.iter("part")}
            extruders[oid][None] = meta(o, "extruder")
    if "Metadata/project_settings.config" in z.namelist():
        colours = json.loads(z.read("Metadata/project_settings.config")).get("filament_colour", [])

    out = []

    def walk(p, oid, t, part_id):
        o = objects(p)[oid]
        mesh = o.find(CORE + "mesh")
        if mesh is not None:
            v = np.array([[float(e.get(a)) for a in "xyz"] for e in mesh.iter(CORE + "vertex")])
            f = np.array([[int(e.get(a)) for a in ("v1", "v2", "v3")] for e in mesh.iter(CORE + "triangle")])
            out.append((part_id, trimesh.Trimesh(v, f, process=False).apply_transform(t)))
            return
        for c in o.iter(CORE + "component"):
            walk(c.get(PROD + "path") or p, c.get("objectid"), t @ matrix(c.get("transform")), c.get("objectid"))

    main = "/3D/3dmodel.model"
    objects(main)
    for i, item in enumerate(docs[main][1].iter(CORE + "item")):
        start, oid = len(out), item.get("objectid")
        walk(main, oid, matrix(item.get("transform")), None)
        object_name = names.get(oid) or f"Object {i + 1}"
        for k in range(start, len(out)):
            part_name = parts.get(oid, {}).get(out[k][0]) or f"part {out[k][0]}"
            if len(out) - start == 1:
                label = object_name
            elif part_name.startswith(object_name):  # Bambu often names parts after the object
                label = part_name
            else:
                label = f"{object_name} / {part_name}"
            ext = extruders.get(oid, {}).get(out[k][0]) or extruders.get(oid, {}).get(None)
            colour = colours[int(ext) - 1] if ext and 0 < int(ext) <= len(colours) else None
            out[k] = (label, out[k][1], colour)
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
