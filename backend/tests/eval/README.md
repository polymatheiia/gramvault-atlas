# Classifier eval

An accuracy benchmark for `gramvault.ai.classifier` (the keyword vote and
the LLM re-label pass) against a real hand-labelled set: the 1814 saved
items the reels-workflow run sorted into the 14 categories by hand.

## Data (not committed)

The dataset is real personal Instagram content, so it is **gitignored**
(`backend/tests/eval/data/`). Provide it one of two ways:

- drop `categories_final.csv` and `items_raw.json` into
  `backend/tests/eval/data/`, or
- point at copies elsewhere:

  ```sh
  export GRAMVAULT_EVAL_LABELS=~/reels-workflow/categories_final.csv
  export GRAMVAULT_EVAL_ITEMS=~/reels-workflow/items_raw.json
  ```

`categories_final.csv` columns: `id,category,media_type,username,taken_at,
shortcode,permalink,caption`. `items_raw.json` is a list (or id-keyed
object) of records with `id, caption, hashtags, transcript, vision`.

When the data is absent every eval test skips.

## Running

```sh
pytest -m eval -s                        # keyword pass (free, offline)
GRAMVAULT_EVAL_LLM=1 pytest -m eval -s    # also the LLM pass (uses the configured `categorize` provider — costs money on an API)
```

`-s` surfaces the per-category precision/recall table and the confusion
list. The `eval` marker is filtered out of the normal run
(`addopts = "-m 'not eval'"` in `pyproject.toml`), so CI and
`pytest backend/tests` never touch this.

## What the asserts guard

Only gross regressions — the keyword floor at ≥45%, the LLM pass at ≥60%,
and that `needs_review` guesses really are less accurate than confident
ones. The captured numbers are the actual deliverable; read them when you
touch `classifier_keywords.py` or the prompt.
