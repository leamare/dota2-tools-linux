"""Pre-run script: no swarms of tiny creatures.

A map's ambient effects -- flies, fireflies, bugs -- are sprites a unit or
two across that a renderer keeps at a minimum size on screen. From the
render's 240000 units up that minimum is many times their size, and a swarm
of them draws as a dark (or glowing) blot that wanders from frame to frame:
the black blot over dota_alpha_883_0's river is particles/alpha/
world_creature_fx/bugs_swamp.vpcf. In the render addon each such effect
placed in the map is replaced by an empty one; the map and its addon keep
theirs.

Which: sprite effects whose particles are at most `max_radius` units
(settings.json, render.flies.max_radius, default 2) and that keep a minimum
screen size, plus any listed in render.flies.hide.
"""
import re

from dota2tools import mapinfo

EMPTY = ('<!-- kv3 encoding:text:version{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d} '
         'format:generic:version{7412167c-06e9-4698-aff2-e63eb59037e7} -->\n'
         '{\n\t_class = "CParticleSystemDefinition"\n\tm_nMaxParticles = 1\n}\n')


def effect_text(ctx, name):
    """An effect's definition as text: its source in the addon's content if
    there, else its compiled form read back."""
    if ctx.addon:
        src = ctx.tools.root / "content" / "dota_addons" / ctx.addon / name
        if src.exists():
            return src.read_text(encoding="utf-8", errors="replace")
    data = mapinfo.read(ctx.tools, ctx.addon, ctx.map, name + "_c")
    return mapinfo.s2v_text(data, ".vpcf_c") if data else ""


def tiny(text, max_radius):
    radii = [float(v) for v in re.findall(r"m_flConstantRadius\s*=\s*([\d.]+)", text)]
    mins = [float(v) for v in re.findall(r"m_flMinSize\s*=\s*([\d.]+)", text)]
    return bool(radii) and max(radii) <= max_radius and any(v > 0 for v in mins) and "C_OP_RenderSprites" in text


def run(ctx):
    max_radius = float(ctx.options.get("max_radius", 2.0))
    listed = set(ctx.options.get("hide", []))
    names = sorted({e["effect_name"].split('"')[-1] for e in mapinfo.entities(ctx.tools, ctx.addon, ctx.map)
                    if e.get("effect_name", "").split('"')[-1].endswith(".vpcf")})
    hidden = []
    for name in names:
        if name in listed or tiny(effect_text(ctx, name), max_radius):
            ctx.write(name, EMPTY)
            ctx.compile([name])
            hidden.append(name)
    ctx.log("flies: " + (", ".join(n.split("/")[-1] for n in hidden) + " left out" if hidden else "none in this map"))
