# Current versus Baseline Strength: controlled chronological experiment

**Recommendation: retain the production 0.6 Current / 0.4 Baseline near-kickoff blend. Do not deploy a new coefficient or subgroup-specific rule from this experiment.**

The broad reconstruction selected 0.6 on validation, and it also minimizes held-out log loss and Brier score. This is evidence against changing the coefficient, not prospective proof that the coefficient is optimal. No production formulas or constants were changed.

## Evidence and design

- Validation: 2023-07-01 through 2024-06-30, **19,239 fixtures**.
- Held-out: 2024-07-01 onward through the query date, **43,658 fixtures**.
- Only completed regulation-time FT matches with the required historical ranks and stored prediction components. Every weight uses exactly the same accepted fixtures within a cohort.
- Grid: 0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0 Current; Baseline receives the remainder.
- Candidate selection uses validation log loss. Held-out grids and subgroups are diagnostic, not permission to retune on the test set.
- This tests a fixed near-kickoff blend, not the production year-ahead interpolation curve.
- Database access used an explicitly READ ONLY, REPEATABLE READ transaction. API-Football calls: **zero**. Immutable match snapshots: **0**; immutable odds observations: **0**.

**Reconstruction limitation:** historical rank inputs have been rebuilt, whereas many old prediction rows have not. The broad replay uses rebuilt historical Current/Baseline ranks while freezing each stored prediction’s other contributions: its residual goal margin and home×away xG product. Changing the rank margin through `project()` preserves that product. Unit tests confirm equivalence to the full prediction formula when inputs are internally consistent. Stored injury/lineup/home-edge effects are not retuned. The experiment cannot prove these reconstructed inputs were known before the event or remove historical model/data-revision leakage.

## Held-out grid

Lower log loss, Brier and goal-difference MAE are better. Brier is the sum across Home/Draw/Away classes. ECE is mean classwise, equal-width 0.1-bin expected calibration error; it is bin-sensitive, not a sole selection criterion.

| Current | Validation log loss | Test log loss | Test Brier | Accuracy | GD MAE | ECE | 95% CI: Δ log loss vs 0.6 |
|---:|---:|---:|---:|---:|---:|---:|---|
| 0.0 | 1.001058 | 1.000505 | 0.597623 | 50.90% | 1.299160 | 0.00983 | +0.005318 to +0.007718 |
| 0.2 | 0.997614 | 0.996973 | 0.595130 | 51.24% | 1.292427 | 0.01131 | +0.002193 to +0.003770 |
| 0.4 | 0.995503 | 0.994804 | 0.593600 | 51.45% | 1.288585 | 0.01148 | +0.000423 to +0.001216 |
| 0.5 | 0.994948 | 0.994228 | 0.593195 | 51.54% | 1.287676 | 0.01054 | +0.000043 to +0.000441 |
| 0.6 | 0.994730 | 0.993990 | 0.593031 | 51.54% | 1.287474 | 0.00982 | +0.000000 to +0.000000 |
| 0.7 | 0.994846 | 0.994087 | 0.593102 | 51.57% | 1.288045 | 0.00884 | -0.000106 to +0.000291 |
| 0.8 | 0.995294 | 0.994514 | 0.593406 | 51.48% | 1.289349 | 0.00772 | +0.000115 to +0.000912 |
| 1.0 | 0.997162 | 0.996357 | 0.594697 | 51.23% | 1.294010 | 0.00646 | +0.001534 to +0.003144 |

Intervals use 2,000 paired UTC-week cluster bootstrap draws (seed 20260926). Negative deltas favour the alternative. They are pointwise, not adjusted for eight weights or many subgroups; shared clubs and season-level dependence remain limitations.

The 0.7 setting improves accuracy slightly but worsens log loss and Brier. Its loss difference is small and its interval crosses zero. No alternative reaches the predeclared practical review threshold of a 0.002 held-out log-loss improvement. Both later calendar periods also favour 0.6 on log loss.

Calibration does not tell exactly the same story: binned ECE decreases at larger Current weights, but log loss and Brier worsen. ECE alone can favour less discriminating forecasts and depends on bins. The recommendation therefore uses the primary proper scores alongside the full calibration tables, not accuracy or ECE alone.

## Temporal stability

| Period | n | Lowest-loss weight | Loss at 0.6 | Loss at 0.7 |
|---|---:|---:|---:|---:|
| 2024-25 | 19,662 | 0.6 | 0.991852 | 0.991948 |
| 2025-onwards | 23,996 | 0.6 | 0.995742 | 0.995839 |

## Subgroups

Exploratory and overlapping; subgroup rows must not be summed. Promotion/relegation requires observed prior-season membership in an explicitly mapped domestic tier. Established top flight requires the same top league in both prior seasons. Early season means either team has fewer than five prior finished league matches that season. Large divergence means either team differs by at least 100 rank points. Missing membership is not treated as evidence of a move.

| Cohort | Validation n | Test n | Validation choice | Test minimum (exploratory) | Δ loss, validation choice vs 0.6 | 95% CI |
|---|---:|---:|---:|---:|---:|---|
| early_season_first_5 | 4,590 | 11,994 | 0.6 | 0.5 | +0.000000 | +0.000000 to +0.000000 |
| established_top_flight | 1,900 | 4,125 | 0.7 | 0.5 | +0.000563 | +0.000014 to +0.001123 |
| large_divergence_100_points | 10 | 39 | 0.5 | 1.0 | +0.014853 | -0.002173 to +0.032307 |
| promoted | 805 | 1,832 | 0.8 | 0.7 | +0.000430 | -0.001733 to +0.002576 |
| recently_relegated | 1,036 | 2,132 | 0.6 | 0.8 | +0.000000 | +0.000000 to +0.000000 |

Large-divergence matches are especially sparse and club-clustered. Apparent subgroup optima are hypothesis generators, not deployable coefficients. Relegated-team and early-season effects need prospective or independently held-out confirmation; do not build conditional production weights from these tables.

## Competition breakdown

All competition grids and calibration counts are in `results.json`. The table below shows the largest cohorts; test-minimum values are exploratory.

| Competition | Test n | Test minimum | Loss at 0.6 | Test minimum loss |
|---|---:|---:|---:|---:|
| FA Cup (45) | 1,856 | 0.4 | 0.978257 | 0.977656 |
| National League - North (50) | 1,216 | 0.6 | 1.023916 | 1.023916 |
| National League (43) | 1,215 | 0.8 | 1.020468 | 1.019759 |
| Liga Profesional Argentina (128) | 1,212 | 0.4 | 1.064747 | 1.063593 |
| Championship (40) | 1,207 | 0.5 | 1.043706 | 1.043441 |
| National League - South (51) | 1,207 | 0.5 | 1.043467 | 1.043260 |
| League Two (42) | 1,197 | 0.7 | 1.054239 | 1.054084 |
| League One (41) | 1,196 | 0.5 | 1.031368 | 1.031252 |
| Major League Soccer (253) | 1,139 | 0.6 | 1.031442 | 1.031442 |
| Segunda División (141) | 1,001 | 0.8 | 1.051860 | 1.050942 |
| UEFA Europa Conference League (848) | 991 | 0.5 | 0.936138 | 0.935899 |
| Serie A (71) | 913 | 0.8 | 1.009734 | 1.009260 |
| Serie B (136) | 829 | 0.6 | 1.064029 | 1.064029 |
| La Liga (140) | 829 | 0.2 | 0.972428 | 0.970653 |
| Serie A (135) | 810 | 0.6 | 0.986007 | 0.986007 |

## Rapid improvers and decliners: descriptive checks only

These examples are selected mechanically by maximum absolute Current-minus-Baseline divergence per team, not by whether a weight predicts their results well. No coefficient was tuned around them. A historical rank divergence is a model signal, not a causal claim about the club.

| Direction | Team | Fixture | Date | Current | Baseline | Difference |
|---|---|---:|---|---:|---:|---:|
| decliners | Adana Demirspor | 1238179 | 2025-05-25 | 671.2 | 796.4 | -125.2 |
| decliners | Morecambe | 1399399 | 2025-10-21 | 562.6 | 675.5 | -112.9 |
| decliners | Girona | 1390869 | 2025-09-23 | 842.3 | 937.7 | -95.4 |
| improvers | AFC Telford United | 1510231 | 2026-01-31 | 653.5 | 548.9 | +104.7 |
| improvers | Paris FC | 1552762 | 2026-09-12 | 987.6 | 874.6 | +113.0 |
| improvers | Como | 1223805 | 2025-01-25 | 968.1 | 847.5 | +120.6 |

## Sensitivity and missing comparisons

An exact-compatibility sensitivity required stored predictions to match rebuilt historical ranks at 0.6/0.4 and today’s probability formula. It retained only 1,439 validation and 2,246 test fixtures, excluding 59,212 mismatched rows. It selected 0.7 on validation, but 0.7 worsened held-out log loss by 0.000567; its apparent test optimum 0.4 improved by only 0.000401, with an interval crossing zero. This tiny, selected cohort is not representative. Results are retained in `compatible_results.json`, not substituted for the broad analysis.

**Bookmaker paired sample: n=0.** There are no immutable timestamped odds observations yet. Current/latest odds cannot establish what was available on the same historical fixtures at prediction time; no market superiority claim is possible.

## Review recommendation

Keep 0.6 Current / 0.4 Baseline near kickoff. Keep the horizon curve and all other production components unchanged. Accumulate prospective snapshots and timestamped prices, then repeat a preregistered paired evaluation. Treat any relegation/early-season conditional weighting as a separate hypothesis requiring validation and adequate independent weeks, not a discovered production rule.

## Reproduction

`run.py` contains the grid, date boundaries, cohort definitions, metrics and paired uncertainty calculation. `results.json` contains all calibration bucket sample sizes, subgroup grids, run/source digest and frozen-input fingerprint. The input extract is stored locally at `.cache/strength_weight_inputs.json.gz` (ignored by Git; no credentials).

```bash
/tmp/thecornerfc-experiment-venv/bin/python experiments/strength_weight/run.py --input-cache .cache/strength_weight_inputs.json.gz
/tmp/thecornerfc-experiment-venv/bin/python experiments/strength_weight/run.py --input-cache .cache/strength_weight_inputs.json.gz --strict-compatible --output experiments/strength_weight/compatible_results.json
python experiments/strength_weight/write_report.py
```

No database writes, API requests or production formula edits were made. Tests verify the counterfactual against the full unchanged prediction formula.
