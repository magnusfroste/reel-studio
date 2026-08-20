from reel_studio.refs import semantic_ref
from reel_studio.schema import Action
from reel_studio.annotations import (
    annotation_hold_seconds,
    annotation_id,
    annotation_script,
    validate_annotation,
)


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
    )
    assert action.type == "click_and_wait"
    assert action.narration_timing == "after_settle"
    assert action.target_text == "Contacts"


def test_annotation_hold_covers_longer_visual_or_audio_window():
    assert annotation_hold_seconds(2.4, 4.2) == 4.2
    assert annotation_hold_seconds(4.2, 2.4) == 4.2


def test_annotation_script_tracks_target_layout_changes():
    script = annotation_script()
    assert "ResizeObserver" in script
    assert "getBoundingClientRect" in script
    assert "window.addEventListener(\"scroll\"" in script
