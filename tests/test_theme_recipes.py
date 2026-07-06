"""Per-theme chrome recipes: the status/footer surfaces are separate roles a
theme configures over its palette, and whatever recipe it names is still
guaranteed enough contrast headroom to bear chrome text."""

from puikit.color import LC_LARGE, max_achievable_lc
from puikit.theme import derive_theme, lift, mix

DARK = dict(background=(30, 30, 30), foreground=(212, 212, 212), muted=(157, 157, 157),
            accent=(0, 122, 204), surface=(48, 48, 52), selection=(10, 105, 178))


def test_footer_is_a_distinct_role_defaulting_to_the_accent_bar():
    t = derive_theme(**DARK)
    assert "footer" in t.surfaces and "status" in t.surfaces
    assert t.surfaces["footer"] == t.surfaces["status"]  # both default to the accent
    assert t.accent2 == t.accent                          # accent2 defaults to accent


def test_accent2_is_settable():
    t = derive_theme(**{**DARK, "accent2": (78, 201, 176)})
    assert t.accent2 == (78, 201, 176)
    assert t.accent == (0, 122, 204)                      # primary accent unchanged


def test_neutral_gray_footer_recipe_leaves_status_as_accent():
    # "accent status bar + gray footer": footer is a blend toward the text.
    gray = mix(DARK["background"], DARK["foreground"], 0.16)
    t = derive_theme(**DARK, surfaces={"footer": gray})
    assert t.surfaces["status"] == DARK["accent"]         # status still the accent
    assert t.surfaces["footer"] == gray                   # dark gray already legible


def test_accent2_blend_recipe_applies_to_both_bars():
    # "both bars = 80/20 background:accent2".
    blend = mix((0, 43, 54), (42, 161, 152), 0.20)
    t = derive_theme(background=(0, 43, 54), foreground=(147, 161, 161),
                     muted=(88, 110, 117), accent=(38, 139, 210), surface=(10, 62, 78),
                     selection=(26, 102, 150), accent2=(42, 161, 152),
                     surfaces={"status": blend, "footer": blend})
    assert t.surfaces["status"] == blend == t.surfaces["footer"]


def test_any_recipe_is_headroom_guaranteed():
    # A theme names a mid-luminance bar the recipe author didn't vet; the
    # post-merge headroom pass still deepens it so it can bear chrome text.
    pale = (190, 150, 250)
    assert max_achievable_lc(pale) < LC_LARGE
    t = derive_theme(**DARK, surfaces={"status": pale, "footer": pale})
    for role in ("status", "footer"):
        assert t.surfaces[role] != pale
        assert max_achievable_lc(t.surfaces[role]) >= LC_LARGE


def test_lift_recipe_helper_is_exported_and_usable():
    raised = lift((48, 48, 52), 0.12)      # a raised panel shade, as derive_theme uses
    assert raised != (48, 48, 52)
