# The Trader — Self-Learning Model Architecture

> How the bots' "AI" works, why it's built this way, and the staged upgrade
> path. No paid LLM is used anywhere — the model is local, free, and trains on
> the bots' own trade outcomes.

## What it is

Each bot has its **own** win-probability model (`app/ml/model.py::WinProbModel`).
Every closed trade becomes one labeled tabular example — ~12 engineered
indicator features (`app/ml/features.py`) → label **win = 1 / loss = 0**. The
model retrains on that growing history and gates entries: a scanned setup is
taken only if `predicted win-probability ≥ min_win_prob` (default 0.55). So the
system **learns from its own mistakes** as it trades.

Surfaced in the dashboard's **AI Model** tab and `GET /api/model`:
model type, **#parameters**, **#training samples**, accuracy / AUC / Brier,
calibration status, and a **Train more** button (`POST /api/model/train`).

## Why this design (the research)

For **small, growing, time-ordered tabular data**, gradient-boosted decision
trees beat neural networks — decisively below ~1k samples and generally up to
~10k. Evidence:

- **Grinsztajn, Oyallon & Varoquaux 2022 (NeurIPS)** — tree models still
  outperform deep nets on typical (~10k) tabular data; NNs are biased to
  over-smooth functions and hurt by uninformative features.
- **Shwartz-Ziv & Armon 2022 (Information Fusion)** — *"Deep Learning Is Not
  All You Need"*: XGBoost beats TabNet/NODE/DNF-Net across datasets with less
  tuning; only a DL **+** XGBoost ensemble helped.
- **scikit-learn calibration docs** — use Platt/**sigmoid** over isotonic when
  calibration samples ≪ 1000 (isotonic overfits small data).
- **TabPFN** (Hollmann et al., Nature 2024) beats GBDTs on <10k rows — but its
  weights are **non-commercial licensed**, so it's disqualified for a live bot.
- **RL (DQN/PPO)** is sample-hungry, non-stationary-sensitive, and overfit-prone
  at retail/testnet scale — inappropriate now.

**Verdict:** the model that actually "thinks and finds the best solution" for
this data is a **calibrated, recency-weighted, gradient-boosted tree with an
honest abstain gate** — not a neural network (yet).

## Current implementation

- **Core:** LightGBM (`LGBMClassifier`, shallow: `max_depth=4`, `num_leaves=15`,
  `reg_lambda=1`, `class_weight="balanced"`). Falls back XGBoost → sklearn
  LogisticRegression if LightGBM isn't installed.
- **Recency weighting:** linear weights (older ≈0.5 → newest ≈1.0) so recent
  regime counts more without hard-forgetting.
- **Calibration:** Platt/**sigmoid** (`CalibratedClassifierCV` + `FrozenEstimator`)
  fit on the **time-ordered** validation tail, so `win_prob ≥ 0.55` is a real
  probability. Applied once the tail has ≥20 both-class samples.
- **Abstain gate:** below `MIN_SAMPLES_TO_START` (50) closed trades, or when the
  history is single-class (e.g. all-losses cold start), the model reports
  *warming up* and the bot trades on pure indicator rules. A model trained on
  too few / one-class samples is noise.
- **No lookahead:** time-ordered train/val split, scaler fit on train slice only.
- **Metrics:** accuracy, ROC-AUC, **Brier** (calibration), and an effective
  **parameter count** — GBDT = total leaf count across trees; linear = coef+intercept.

## Staged upgrade path (as data grows)

| Trades (per bot) | Model | Notes |
|---|---|---|
| < 50 | none (abstain) | indicator rules; log features+outcomes |
| 50–1,000 | **LightGBM + Platt calibration** ← *we are here* | recency window, time-split |
| 1,000–10,000 | GBDT + light Optuna tuning; maybe isotonic calibration | trees still win |
| > 10,000 | GBDT backbone **+ optional small MLP / FT-Transformer as an ensemble member** | config-gated, only if walk-forward backtest shows it improves calibrated AUC |

**Optional future (documented, not yet built):**
- `river` online logistic-regression + **ADWIN** drift detector as a
  fast-adapting secondary signal ("learn from every trade instantly").
- Neural-net ensemble member, activated only past ~10k trades and only if it
  beats the GBDT on out-of-sample calibrated AUC.

## Honest expectations

Profitability is never guaranteed. The model only helps once each bot has 50+
**both-class** closed trades; until then it abstains. Accuracy is measured on a
time-ordered holdout (walk-forward), never a shuffled split, so the numbers are
not lookahead-inflated. The grid bot currently shows *warming up* at 80 samples
because its history is all-losses — the classifier correctly refuses to train on
a single class.
