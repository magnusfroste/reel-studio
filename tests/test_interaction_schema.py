from reel_studio.refs import semantic_ref
from reel_studio.schema import Action


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
