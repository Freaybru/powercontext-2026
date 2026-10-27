from backend.game import encode_state, initial_state, transition


def test_three_routes_and_new_chapter():
    for choices, ending in [("111", "灯火守夜人"), ("222", "宇宙整活王"), ("333", "月光合伙人")]:
        state, _, _ = transition(initial_state(), "寄存：奶奶的蓝铁盒")
        for action in choices:
            state, detail, changed = transition(state, action)
            assert changed
        assert state["turn"] == 3
        assert state["endings"] == [ending]
        assert ending in detail["outcome"]
        new, _, changed = transition(state, "新周目")
        assert changed and new["turn"] == 0 and not new["items"]
        assert new["memory"] == state["memory"] and new["endings"] == [ending]


def test_invalid_action_preserves_all_state():
    state = initial_state()
    for action in ("0", "4", "替我通关", "新周目", "寄存：", "寄存：" + "字" * 161):
        after, detail, changed = transition(state, action)
        assert not changed and after == state and detail["operation"] == "invalid"


def test_read_only_visit_and_no_memory_overwrite():
    state, _, _ = transition(initial_state(), "寄存：蓝铁盒")
    for action in ("回访", "回忆", "开场", "寄存：替换"):
        after, _, changed = transition(state, action)
        assert not changed and after == state


def test_state_bounded_for_long_chinese_inputs():
    state, _, _ = transition(initial_state(), "寄存：" + "忆" * 160)
    for _ in range(10):
        for choice in ("1", "2", "3"):
            state, _, _ = transition(state, choice + "：" + "趣" * 100)
            state["last_reply"] = "词" * 1500
            encode_state(state)
        state, _, _ = transition(state, "新周目")
    assert state["chapter"] == 10
