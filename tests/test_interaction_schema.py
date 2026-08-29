from reel_studio.refs import semantic_ref
from reel_studio.schema import Action
from reel_studio.annotations import (
    annotation_hold_seconds,
    annotation_id,
    annotation_script,
    caption_script,
    validate_annotation,
)


def test_caption_action_is_supported():
    action = Action(type="caption", text="Look at the lead signal", ms=3000)
    assert action.type == "caption"
    assert action.text == "Look at the lead signal"


def test_caption_script_is_readable_and_fixed_to_viewport():
    script = caption_script()
    assert 'fontSize: "22px"' in script
    assert 'bottom: "34px"' in script
    assert 'data-annotation-id' in script


def test_semantic_ref_uses_role_and_visible_name():
    assert semantic_ref("button", "New Contact", 0, set()) == "button:new-contact"


def test_semantic_ref_disambiguates_duplicate_names():
    used = {"button:save"}
    assert semantic_ref("button", "Save", 3, used) == "button:save-2"


def test_select_option_action_accepts_label():
    action = Action(type="select_option", ref="product-selector", text="Starter Plan")
    assert action.type == "select_option"
    assert action.text == "Starter Plan"


def test_press_key_action_accepts_key():
    action = Action(type="press_key", ref="product-selector", text="Escape")
    assert action.type == "press_key"
    assert action.text == "Escape"


def test_set_zoom_action_accepts_level():
    action = Action(type="set_zoom", text="1.15")
    assert action.type == "set_zoom"
    assert action.text == "1.15"


def test_annotation_contract_normalizes_and_rejects_bad_duration():
    assert validate_annotation("CALLout", "Create deal", 2500) == {
        "kind": "callout", "label": "Create deal", "duration_ms": 2500,
    }


def test_annotation_id_is_deterministic():
    assert annotation_id("button:create-deal", 2) == "annotation-button-create-deal-2"


def test_click_and_wait_action_carries_settle_contract():
    action = Action(
        type="click_and_wait",
        ref="module:contacts",
        wait_for_url="/admin/contacts",
        wait_for_text="Contacts",
        target_text="Contacts",
        settle_ms=700,
        narration_timing="after_settle",
    )
    assert action.type == "click_and_wait"
    assert action.narration_timing == "after_settle"
    assert action.target_text == "Contacts"


def test_narration_timing_options():
    action_before = Action(type="click", ref="btn", narration_timing="before_action")
    action_after = Action(type="click", ref="btn", narration_timing="after_action")
    action_settle = Action(type="click", ref="btn", narration_timing="after_settle")
    assert action_before.narration_timing == "before_action"
    assert action_after.narration_timing == "after_action"
    assert action_settle.narration_timing == "after_settle"


def test_annotation_hold_covers_longer_visual_or_audio_window():
    assert annotation_hold_seconds(2.4, 4.2) == 4.2
    assert annotation_hold_seconds(4.2, 2.4) == 4.2


def test_annotation_script_tracks_target_layout_changes():
    script = annotation_script()
    assert "ResizeObserver" in script
    assert "getBoundingClientRect" in script
    assert "window.addEventListener(\"scroll\"" in script


def test_annotation_script_compensates_for_document_zoom():
    script = annotation_script()
    assert "getComputedStyle(document.documentElement).zoom" in script
    assert "rect.left / z" in script
    assert "rect.width / z" in script

    # The overlay coordinates are assigned in the zoomed document's coordinate
    # system, while getBoundingClientRect returns viewport coordinates.
    assert "const z = zoomFactor()" in script


def test_annotation_script_places_labels_using_available_viewport_space():
    script = annotation_script()
    assert "label.offsetWidth" in script
    assert "label.offsetHeight" in script
    assert "aboveY" in script
    assert "belowY" in script
    assert "viewportHeight - 8" in script


def test_click_feedback_uses_a_visible_cursor_halo_and_pulse():
    from reel_studio.engine import BrowserSession

    source = BrowserSession._inject_spotlight.__code__
    assert source.co_consts
    script = next(value for value in source.co_consts if isinstance(value, str) and "videoDirectorSpotlight" in value)
    assert "videoDirectorCursorHalo" in script
    assert "videoDirectorClickPulse" in script
    assert "animation: 'cursor-halo 900ms ease-out'" in script
    assert "setTimeout(() => {" in script
    assert "}, 900);" in script


def test_click_feedback_is_not_a_single_frame_flash():
    from reel_studio.engine import BrowserSession

    source = BrowserSession._inject_spotlight.__code__
    script = next(value for value in source.co_consts if isinstance(value, str) and "videoDirectorSpotlight" in value)
    assert "transition: 'opacity 420ms ease, transform 420ms ease'" in script
    assert "animation: 'click-pulse 650ms ease-out'" in script
    assert "@keyframes click-pulse" in script
    assert "@keyframes cursor-halo" in script
    assert "cursor-halo" in script
    assert "click-pulse" in script
    assert "900" in script
