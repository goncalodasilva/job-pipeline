from tests.job_fixtures import generate_jobs


def test_job_generator_makes_1000_fictional_jobs_deterministically():
    first = generate_jobs()
    assert len(first) == 1000 and first == generate_jobs()
    assert len({j["id"] for j in first}) == 1000
    assert all("Fictional" in j["title"] and "Fictional" in j["company_name"] for j in first)
    assert len({j["company_id"] for j in first}) == 20
    assert len({j["industry"] for j in first}) == 5
    assert len({j["work_mode"] for j in first}) == 3
    assert len({j["seniority"] for j in first}) == 4
    assert len({j["fit_band"] for j in first}) == 4
    first[0]["title"] = "Changed only in this call"
    assert generate_jobs()[0]["title"] != "Changed only in this call"
