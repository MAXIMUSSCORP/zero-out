from tests.test_web import web  # noqa: F401
from tests.test_phone_web import LOCAL_ORIGIN, clip, env, fake, local_client  # noqa: F401
from zeroout.menu_text import menu_lines
from zeroout.models import Observation, Option


def test_menu_lines_is_a_filter_of_the_apps_templates(env, monkeypatch):
    made = []

    class Spy(web.Jinja2Templates):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(web, "Jinja2Templates", Spy)
    env.build()
    assert len(made) == 1 and made[0].env.filters["menu_lines"] is menu_lines
    out = made[0].env.from_string("{% for l in t|menu_lines(['1']) %}{{ l.key }}:{{ l.text }};{% endfor %}")
    assert out.render(t="Hi. Press 1 now.") == "None:Hi.;1:Press 1 now.;"


def test_clip_response_carries_lines(env, fake):
    fake.obs = Observation("For billing press 1, for support press 2.",
                           [Option("1", "billing"), Option("2", "support")], "menu", 4.0)
    j = clip(local_client(env), env, path="").json()
    assert j["heard"]["lines"] == [
        {"text": "For billing press 1", "key": "1"}, {"text": "for support press 2.", "key": "2"}]
    assert j["heard"]["prompt_text"] == "For billing press 1, for support press 2."


def test_clip_lines_only_count_the_options_heard(env, fake):
    fake.obs = Observation("Welcome. Press 1 for billing. Press 7 for fun.", [Option("1", "billing")], "menu", 4.0)
    j = clip(local_client(env), env, path="").json()
    assert [(x["text"], x["key"]) for x in j["heard"]["lines"]] == [
        ("Welcome.", None), ("Press 1 for billing.", "1"), ("Press 7 for fun.", None)]


def test_clip_lines_for_empty_prompt_is_empty(env, fake):
    fake.obs = Observation("", [], "hold", 1.0)
    assert clip(local_client(env), env, path="").json()["heard"]["lines"] == []


def test_ask_shows_closest_match_line_only_when_closest(env):
    from tests.test_web import mapped
    mapped(env)
    env.answer = {"path": "2", "explanation": "Press 2 for an agent", "match": "closest"}
    t = env.client.post(f"/c/{env.cid}/ask", data={"question": "a person"}).text
    assert "Closest match on this map" in t and t.index("Closest match") < t.index("Press 2 for an agent")
    env.answer = {"path": "2", "explanation": "Press 2 for an agent", "match": "exact"}
    assert "Closest match" not in env.client.post(f"/c/{env.cid}/ask", data={"question": "agent"}).text
