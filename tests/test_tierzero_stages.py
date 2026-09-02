from clients.tierzero import driver


def test_tierzero_skips_diagnosis_when_attempt_starts_at_mitigation(monkeypatch, tmp_path):
    submissions: list[tuple[str, str]] = []
    prompts: list[str] = []

    monkeypatch.setattr(driver, "TIERZERO_ORG_API_KEY", "test-key")
    monkeypatch.setattr(driver, "AGENT_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(
        driver, "wait_for_stage", lambda stages, timeout: "mitigation" if "mitigation" in stages else "done"
    )
    monkeypatch.setattr(
        driver,
        "get_app_info",
        lambda: {"app_name": "app", "namespace": "namespace", "descriptions": "description"},
    )
    monkeypatch.setattr(driver, "resolve_problem_id", lambda: "problem")

    def create_interaction(prompt, context=None):
        prompts.append(prompt)
        assert context is None
        return "mitigation-interaction"

    monkeypatch.setattr(driver, "create_interaction", create_interaction)
    monkeypatch.setattr(driver, "poll_interaction", lambda _interaction_id: {"status": "COMPLETED"})
    monkeypatch.setattr(driver, "extract_content", lambda _result: "fixed")
    monkeypatch.setattr(driver, "submit_to_conductor", lambda answer, stage: submissions.append((answer, stage)))

    driver.main()

    assert len(prompts) == 1
    assert submissions == [("", "mitigation")]
    assert not (tmp_path / "diagnosis_result.json").exists()
    assert (tmp_path / "mitigation_result.json").exists()
