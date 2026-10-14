"""Model loading: the 3MF reader and the side-by-side scene builder."""
import io
import json
import zipfile

import numpy as np
import pytest
import trimesh

from model_review_mcp.server import GAP, build_scene, load_3mf

IDENTITY = "1 0 0 0 1 0 0 0 1"
ROTATE_Z_90 = "0 1 0 -1 0 0 0 0 1"  # 3MF row-vector form: x -> y, y -> -x


def mesh_xml(oid, lo, hi):
    box = trimesh.creation.box(bounds=[lo, hi])
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in box.vertices)
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in box.faces)
    return f'<object id="{oid}" type="model"><mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh></object>'


def component(oid, transform=None):
    t = f' transform="{transform}"' if transform else ""
    return f'<component p:path="/3D/Objects/object_1.model" objectid="{oid}"{t}/>'


MODEL = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" '
         'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
         'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
         '<resources>{resources}</resources><build>{build}</build></model>')

# One shared object file with several meshes, the way Bambu Studio stores multi-part objects
SHARED = MODEL.format(resources=mesh_xml(1, [0, 0, 0], [10, 20, 5]) + mesh_xml(2, [0, 0, 0], [2, 2, 2])
                      + mesh_xml(3, [0, 0, 0], [4, 6, 8]), build="")
MAIN = MODEL.format(
    resources=(
        f'<object id="10" type="model"><components>{component(1, IDENTITY + " 5 0 0")}{component(2)}</components></object>'
        f'<object id="11" type="model"><components>{component(3, ROTATE_Z_90 + " 0 0 0")}</components></object>'
        + mesh_xml(12, [0, 0, 0], [1, 1, 1])
        + f'<object id="13" type="model"><components>{component(1)}{component(2)}</components></object>'
    ),
    build=(f'<item objectid="10" transform="{IDENTITY} 100 50 0"/>'
           f'<item objectid="11" transform="{IDENTITY} 0 0 10"/>'
           '<item objectid="12"/><item objectid="13"/>'),
)
MODEL_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="10">
    <metadata key="name" value="Bracket"/>
    <metadata key="extruder" value="2"/>
    <part id="1" subtype="normal_part">
      <metadata key="name" value="Bracket.stl"/>
      <metadata key="extruder" value="1"/>
    </part>
    <part id="2" subtype="normal_part">
      <metadata key="name" value="Insert"/>
    </part>
  </object>
  <object id="11">
    <metadata key="name" value="Knob"/>
    <metadata key="extruder" value="5"/>
    <part id="3" subtype="normal_part">
      <metadata key="name" value="Knob part"/>
    </part>
  </object>
</config>"""
COLOURS = ["#FF0000", "#00FF00FF", "#0000FF"]


def write_3mf(path, main=MAIN, shared=SHARED, settings=MODEL_SETTINGS, colours=COLOURS):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", main)
        if shared:
            z.writestr("3D/Objects/object_1.model", shared)
        if settings:
            z.writestr("Metadata/model_settings.config", settings)
        if colours is not None:
            z.writestr("Metadata/project_settings.config", json.dumps({"filament_colour": colours}))
    return path


def write_stl(path, lo, hi):
    trimesh.creation.box(bounds=[lo, hi]).export(path)
    return path


@pytest.fixture
def project(tmp_path):
    return write_3mf(tmp_path / "Project.3mf")


def summary(parts):
    return [(label, mesh.bounds.tolist(), colour) for label, mesh, colour in parts]


def test_load_3mf_resolves_shared_components_transforms_names_and_colours(project):
    assert summary(load_3mf(project)) == [
        # part extruder 1; the name starts with the object name, so it is used alone
        ("Bracket.stl", [[105, 50, 0], [115, 70, 5]], "#FF0000"),
        # no part extruder: falls back to the object's extruder 2 (alpha byte kept here)
        ("Bracket / Insert", [[100, 50, 0], [102, 52, 2]], "#00FF00FF"),
        # single part: the object name; rotated 90 degrees about Z; extruder 5 has no colour
        ("Knob", [[-6, 0, 10], [0, 4, 18]], None),
        # no metadata: numbered by build item, parts by object id
        ("Object 3", [[0, 0, 0], [1, 1, 1]], None),
        ("Object 4 / part 1", [[0, 0, 0], [10, 20, 5]], None),
        ("Object 4 / part 2", [[0, 0, 0], [2, 2, 2]], None),
    ]


def test_load_3mf_without_metadata(tmp_path):
    main = MODEL.format(resources=mesh_xml(1, [0, 0, 0], [3, 3, 3]), build='<item objectid="1"/>')
    path = write_3mf(tmp_path / "plain.3mf", main=main, shared=None, settings=None, colours=None)
    assert summary(load_3mf(path)) == [("Object 1", [[0, 0, 0], [3, 3, 3]], None)]


def test_load_3mf_extruder_zero_has_no_colour(tmp_path):
    settings = MODEL_SETTINGS.replace('key="extruder" value="1"', 'key="extruder" value="0"')
    labels = summary(load_3mf(write_3mf(tmp_path / "p.3mf", settings=settings)))
    assert labels[0] == ("Bracket.stl", [[105, 50, 0], [115, 70, 5]], None)


def test_build_scene_single_3mf_strips_stl_suffix_and_trims_alpha(project):
    glb, parts, colours, offsets = build_scene([str(project)])
    assert glb[:4] == b"glTF"
    assert parts == ["Bracket", "Bracket / Insert", "Knob", "Object 3", "Object 4 / part 1", "Object 4 / part 2"]
    assert colours == {"Bracket": "#FF0000", "Bracket / Insert": "#00FF00"}
    assert offsets == {name: [0.0, 0.0, 0.0] for name in parts}


def test_build_scene_single_stl(tmp_path):
    glb, parts, colours, offsets = build_scene([str(write_stl(tmp_path / "Box.stl", [0, 0, 0], [1, 2, 3]))])
    assert glb[:4] == b"glTF"
    assert (parts, colours, offsets) == (["Box"], {}, {"Box": [0.0, 0.0, 0.0]})


def test_build_scene_lays_files_out_side_by_side(tmp_path, project):
    a = write_stl(tmp_path / "a.stl", [0, 0, 0], [10, 1, 1])
    b = write_stl(tmp_path / "b.STL", [5, 3, 0], [8, 4, 1])
    glb, parts, colours, offsets = build_scene([str(a), str(b), str(project)])

    assert GAP == 20.0
    b_shift = 10 + GAP - 5           # cursor after a, minus b's minimum x
    p_shift = 8 + b_shift + GAP + 6  # cursor after b, minus the project's minimum x (-6)
    assert b_shift == 25 and p_shift == 59
    assert parts == ["a", "b", "Project: Bracket", "Project: Bracket / Insert", "Project: Knob",
                     "Project: Object 3", "Project: Object 4 / part 1", "Project: Object 4 / part 2"]
    assert offsets == {"a": [0.0, 0.0, 0.0], "b": [25.0, 0.0, 0.0],
                       **{name: [59.0, 0.0, 0.0] for name in parts[2:]}}
    assert colours == {"Project: Bracket": "#FF0000", "Project: Bracket / Insert": "#00FF00"}

    scene = trimesh.load(io.BytesIO(glb), file_type="glb")
    bounds = {name: scene.geometry[geom].bounds for name, (_, geom) in
              ((n, scene.graph[n]) for n in scene.graph.nodes_geometry)}
    assert np.allclose(bounds["b"], [[30, 3, 0], [33, 4, 1]])


def test_build_scene_keeps_only_the_last_copy_of_a_named_object(tmp_path):
    main = MODEL.format(resources=mesh_xml(1, [0, 0, 0], [1, 1, 1]),
                        build=f'<item objectid="1"/><item objectid="1" transform="{IDENTITY} 5 0 0"/>')
    settings = '<config><object id="1"><metadata key="name" value="Cube"/></object></config>'
    path = write_3mf(tmp_path / "copies.3mf", main=main, shared=None, settings=settings, colours=None)
    assert [label for label, _, _ in load_3mf(path)] == ["Cube", "Cube"]

    glb, parts, colours, offsets = build_scene([str(path)])
    # Current behavior, see tidy-project report: both copies share the node name "Cube", so the
    # second copy replaces the first in the scene and only one copy is shown.
    assert parts == ["Cube"]
    assert offsets == {"Cube": [0.0, 0.0, 0.0]}
    scene = trimesh.load(io.BytesIO(glb), file_type="glb")
    assert scene.bounds.tolist() == [[5, 0, 0], [6, 1, 1]]
