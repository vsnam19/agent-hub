from agenthub.skillweekly import Report


def test_report_says_bumped_not_released():
    report = Report(date="2026-10-12", released=["learned-worker 0.1.1"])
    rendered = report.render()
    assert "## Bumped (not released)" in rendered
    assert "## Released" not in rendered
    assert "- learned-worker 0.1.1" in rendered

    report.outcome = "released"
    rendered_released = report.render()
    assert "## Released" in rendered_released
    assert "## Bumped (not released)" not in rendered_released
    assert "- learned-worker 0.1.1" in rendered_released


def test_report_groups_single_categories():
    report = Report(date="2026-10-12")
    report.metrics = {
        "findings_per_category": {
            "cat_a": 5,
            "cat_b": 3,
            "cat_c": 2,
            "cat_d": 1,
            "cat_e": 1,
            "cat_f": 1,
        }
    }
    rendered = report.render()
    assert "- finding category: 5× cat_a" in rendered
    assert "- finding category: 3× cat_b" in rendered
    assert "- finding category: 2× cat_c" in rendered
    assert "cat_d" not in rendered
    assert "cat_e" not in rendered
    assert "cat_f" not in rendered
    assert "- finding categories seen once: 3" in rendered


def test_learned_brief_requires_baseline_failing_case():
    import importlib.resources

    text = importlib.resources.files("agenthub").joinpath("templates/learned-brief.md").read_text("utf-8")
    assert "no-plugin baseline fails" in text
    assert "prompt.md" in text


def test_evaluate_runs_every_skill_before_stopping(tmp_path):
    from tests.test_skillweekly import Env, eval_json

    env = Env(
        tmp_path,
        director=True,
        evals=[eval_json(0.5, 0.8), eval_json(0.4, 0.9)],
    )
    report = env.run()
    assert report.exit_code == 1
    assert "below baseline" in report.stop_reason
    assert "bar" in report.stop_reason
    assert "foo" in report.stop_reason
    assert "bar" in report.evals
    assert "foo" in report.evals
    assert (env.out / "eval-bar.json").is_file()
    assert (env.out / "eval-foo.json").is_file()
