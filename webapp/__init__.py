"""Web layer for Report Automation.

This package wraps the existing, frozen X/Twitter CLI pipeline (run.py + src/)
and the new Influencer pipeline (influencer/) in a small multi-user web app.

Nothing in here imports-and-mutates the frozen X modules: the pipelines are
invoked as subprocesses against a per-job copy of the code, so concurrent jobs
never share an output folder.
"""
