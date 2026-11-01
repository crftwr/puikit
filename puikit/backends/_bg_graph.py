"""The pass graph of a :class:`~puikit.background.Shader`, resolved for a renderer.

Shared by the Metal (``_metal.py``) and Direct3D 11 (``_d3d_shader.py``)
renderers, so the two agree on what a graph means and only differ in how they
allocate a target, bind an input and draw: :func:`plan` checks the graph and
turns names into slots, :func:`once_work` lays out the bake, and
:func:`pass_uniforms` packs what each pass is told about itself.

A shader with no passes is the single-pass form, planned as one frame pass —
``puikit_bg_fragment`` to the screen with every texture bound by position — so
a renderer has one code path for both.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..background import MAX_PASS_INPUTS, SHADER_ENTRY

#: Bytes in ``PassUniforms``: params, target, and one float4 per input slot.
PASS_UNIFORM_BYTES = 16 * (2 + MAX_PASS_INPUTS)

#: Slices of ``once`` passes run per frame while a background bakes: enough to
#: finish a 144-slice volume in about half a second, few enough to hide in a frame.
BAKE_SLICES_PER_FRAME = 8

#: What an input refers to: ``("texture", i)`` is ``Shader.textures[i]``,
#: ``("target", j)`` is ``Plan.targets[j]``.
Ref = "tuple[str, int]"


@dataclass(frozen=True)
class Step:
    """One pass, resolved: ``output`` a target index or ``None`` for the screen."""

    entry: str
    output: "int | None"
    inputs: "tuple[tuple[str, int], ...]"
    params: "tuple[float, ...]"
    once: bool


@dataclass(frozen=True)
class Plan:
    targets: tuple
    once: "tuple[Step, ...]"
    frame: "tuple[Step, ...]"
    error: "str | None" = None

    @property
    def entries(self) -> "set[tuple[str, str | None]]":
        """Every (entry, output format) a renderer must build a pipeline for;
        ``None`` is the screen."""
        return {(s.entry, None if s.output is None else self.targets[s.output].format)
                for s in self.once + self.frame}


def _failed(error: str) -> Plan:
    return Plan((), (), (), error)


def plan(shader) -> Plan:
    """Check ``shader``'s graph and resolve it. A graph that does not hold
    together comes back with ``error`` set and nothing to run."""
    textures = tuple(getattr(shader, "textures", ()) or ())
    targets = tuple(getattr(shader, "targets", ()) or ())
    passes = tuple(getattr(shader, "passes", ()) or ())
    if not passes:
        if targets:
            return _failed("targets without passes")
        step = Step(SHADER_ENTRY, None, tuple(("texture", i) for i in range(len(textures))),
                    (0.0,) * 4, False)
        return Plan((), (), (step,))

    names: "dict[str, tuple[str, int]]" = {}
    for i, tex in enumerate(textures):
        if tex.name:
            if tex.name in names:
                return _failed(f"texture name {tex.name!r} repeats")
            names[tex.name] = ("texture", i)
    for j, target in enumerate(targets):
        if not target.name:
            return _failed("a target has no name")
        if target.name in names:
            return _failed(f"name {target.name!r} repeats")
        names[target.name] = ("target", j)

    baked: "set[int]" = set()       # targets a once pass wrote
    written: "set[int]" = set()     # targets written before the frame pass at hand
    once, frame = [], []
    for k, p in enumerate(passes):
        last = k == len(passes) - 1
        where = f"pass {k} ({p.entry})"
        if p.output is None:
            if not last or p.once:
                return _failed(f"{where}: only the last pass draws to the screen")
            out = None
        else:
            if last:
                return _failed(f"{where}: the last pass must draw to the screen")
            ref = names.get(p.output)
            if ref is None or ref[0] != "target":
                return _failed(f"{where}: no target named {p.output!r}")
            out = ref[1]
        if len(p.inputs) > MAX_PASS_INPUTS:
            return _failed(f"{where}: more than {MAX_PASS_INPUTS} inputs")
        refs = []
        for name in p.inputs:
            ref = names.get(name)
            if ref is None:
                return _failed(f"{where}: no texture or target named {name!r}")
            if ref[0] == "target":
                if ref[1] == out:
                    return _failed(f"{where}: reads its own output {name!r}")
                ready = baked if p.once else baked | written
                if ref[1] not in ready:
                    return _failed(f"{where}: reads {name!r} before any pass writes it")
            refs.append(ref)
        if p.once:
            if targets[out].size is None:
                return _failed(f"{where}: a once pass needs a target with an absolute size")
            baked.add(out)
        elif out is not None:
            if out in baked:
                return _failed(f"{where}: writes {p.output!r}, which a once pass baked")
            written.add(out)
        (once if p.once else frame).append(Step(p.entry, out, tuple(refs), p.params, p.once))
    return Plan(targets, tuple(once), tuple(frame))


def once_work(plan_: Plan) -> "list[tuple[Step, int]]":
    """Every (step, slice) the bake runs, in order: one per slice of a 3D output,
    one for a 2D one."""
    work = []
    for step in plan_.once:
        depth = plan_.targets[step.output].extent(1, 1)[2]
        work.extend((step, z) for z in range(depth))
    return work


def pass_uniforms(step: Step, output: "tuple[int, int, int]", slice_: int,
                  inputs: "list[tuple[int, int, int]]") -> bytes:
    """Pack ``PassUniforms`` (see the preludes) for one draw."""
    values = [*step.params, float(output[0]), float(output[1]), float(output[2]),
              float(slice_)]
    for i in range(MAX_PASS_INPUTS):
        w, h, d = inputs[i] if i < len(inputs) else (0, 0, 0)
        values += [float(w), float(h), float(d), 0.0]
    return struct.pack(f"{len(values)}f", *values)
