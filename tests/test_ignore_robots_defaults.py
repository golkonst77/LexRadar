"""Check entry points through CrawlOptions without performing collection."""

from unittest.mock import patch

import pytest

from lexradar import mvp_batch, pipeline10


@pytest.mark.parametrize("entry,override", [
    (entry, override)
    for entry in ("run", "audit", "process", "batch_cli", "pipeline_cli")
    for override in ([{}] if entry.endswith("cli") else [{}, {"ignore_robots": False}])
])
def test_ignore_robots_reaches_crawl_options(tmp_path, entry, override):
    source = tmp_path / "input.csv"
    source.write_text("url,company_name,email\nhttps://clinic.invalid/,Clinic,\n")
    output = tmp_path / "output"
    row = {"id": "parser-1", "company_name": "Clinic",
           "website": "https://clinic.invalid/", "email": "", "export_approved": False}
    with patch("lexradar.collector.collect", side_effect=RuntimeError("offline stop")) as collect:
        if entry == "run":
            mvp_batch.run(source, output, mode="prepare", allow_site_collection=True, **override)
        elif entry == "audit":
            with pytest.raises(RuntimeError, match="Audit failed"):
                pipeline10.audit(row, {}, output, True, **override)
        elif entry == "process":
            pipeline10.process([row], output, {}, allow_collection=True, **override)
        elif entry == "batch_cli":
            mvp_batch.main([str(source), "--output", str(output), "--mode", "prepare",
                            "--allow-site-collection"])
        else:
            with patch.object(pipeline10, "convert", return_value=(b"", [row] * 10)):
                pipeline10.main([str(source), "--output", str(output), "--allow-site-collection"])
        collect.assert_called_once()
        options = collect.call_args.kwargs["crawl_options"]
        assert options.ignore_robots is override.get("ignore_robots", True)
        assert options.max_requests == 16
        assert options.max_duration_seconds == 60
