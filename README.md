# ITTE MVP

ITTE is a self-evolving risk gate for AI engineering changes.

It intercepts risky prompt, config, model, policy, tool, or code changes before deployment.

## Features

- Pre-deploy AI risk evaluation
- Memory of past changes and outcomes
- Similar historical incident detection
- CI/CD compatible CLI
- SQLite local storage

## Install

```bash
python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
