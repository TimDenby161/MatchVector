"""Render the recorded numerical result; no database access."""
import json
from pathlib import Path
root=Path(__file__).parent
r=json.loads((root/'results.json').read_text())
s=json.loads((root/'compatible_results.json').read_text())
site=json.loads(Path('docs/data/matches.json').read_text())
team_names=site['teams'];competitions=site['competitions']
lines=['# Current versus Baseline Strength: controlled chronological experiment','',
'**Recommendation: retain the production 0.6 Current / 0.4 Baseline near-kickoff blend. Do not deploy a new coefficient or subgroup-specific rule from this experiment.**','',
'The broad reconstruction selected 0.6 on validation, and it also minimizes held-out log loss and Brier score. This is evidence against changing the coefficient, not prospective proof that the coefficient is optimal. No production formulas or constants were changed.','',
'## Evidence and design','',
f'- Validation: 2023-07-01 through 2024-06-30, **{r["validation_n"]:,} fixtures**.',
f'- Held-out: 2024-07-01 onward through the query date, **{r["test_n"]:,} fixtures**.',
'- Only completed regulation-time FT matches with the required historical ranks and stored prediction components. Every weight uses exactly the same accepted fixtures within a cohort.',
'- Grid: 0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0 Current; Baseline receives the remainder.',
'- Candidate selection uses validation log loss. Held-out grids and subgroups are diagnostic, not permission to retune on the test set.',
'- This tests a fixed near-kickoff blend, not the production year-ahead interpolation curve.',
'- Database access used an explicitly READ ONLY, REPEATABLE READ transaction. API-Football calls: **zero**. Immutable match snapshots: **0**; immutable odds observations: **0**.',
'',
'**Reconstruction limitation:** historical rank inputs have been rebuilt, whereas many old prediction rows have not. The broad replay uses rebuilt historical Current/Baseline ranks while freezing each stored prediction’s other contributions: its residual goal margin and home×away xG product. Changing the rank margin through `project()` preserves that product. Unit tests confirm equivalence to the full prediction formula when inputs are internally consistent. Stored injury/lineup/home-edge effects are not retuned. The experiment cannot prove these reconstructed inputs were known before the event or remove historical model/data-revision leakage.','',
'## Held-out grid','',
'Lower log loss, Brier and goal-difference MAE are better. Brier is the sum across Home/Draw/Away classes. ECE is mean classwise, equal-width 0.1-bin expected calibration error; it is bin-sensitive, not a sole selection criterion.','',
'| Current | Validation log loss | Test log loss | Test Brier | Accuracy | GD MAE | ECE | 95% CI: Δ log loss vs 0.6 |',
'|---:|---:|---:|---:|---:|---:|---:|---|']
for w,m in r['test'].items():
 ci=m['log_loss_uncertainty']['ci95']
 lines.append(f'| {w} | {r["validation"][w]["log_loss"]:.6f} | {m["log_loss"]:.6f} | {m["brier"]:.6f} | {m["accuracy"]:.2%} | {m["goal_difference_mae"]:.6f} | {m.get("classwise_ece",0):.5f} | {ci[0]:+.6f} to {ci[1]:+.6f} |')
lines+=['',
'Intervals use 2,000 paired UTC-week cluster bootstrap draws (seed 20260926). Negative deltas favour the alternative. They are pointwise, not adjusted for eight weights or many subgroups; shared clubs and season-level dependence remain limitations.',
'',
'The 0.7 setting improves accuracy slightly but worsens log loss and Brier. Its loss difference is small and its interval crosses zero. No alternative reaches the predeclared practical review threshold of a 0.002 held-out log-loss improvement. Both later calendar periods also favour 0.6 on log loss.','',
'Calibration does not tell exactly the same story: binned ECE decreases at larger Current weights, but log loss and Brier worsen. ECE alone can favour less discriminating forecasts and depends on bins. The recommendation therefore uses the primary proper scores alongside the full calibration tables, not accuracy or ECE alone.', '', '## Temporal stability','',
'| Period | n | Lowest-loss weight | Loss at 0.6 | Loss at 0.7 |','|---|---:|---:|---:|---:|']
for name,g in r['test_periods'].items():
 best=min(g,key=lambda w:g[w]['log_loss'])
 lines.append(f'| {name} | {g["0.6"]["n"]:,} | {best} | {g["0.6"]["log_loss"]:.6f} | {g["0.7"]["log_loss"]:.6f} |')
lines+=['','## Subgroups','',
'Exploratory and overlapping; subgroup rows must not be summed. Promotion/relegation requires observed prior-season membership in an explicitly mapped domestic tier. Established top flight requires the same top league in both prior seasons. Early season means either team has fewer than five prior finished league matches that season. Large divergence means either team differs by at least 100 rank points. Missing membership is not treated as evidence of a move.','',
'| Cohort | Validation n | Test n | Validation choice | Test minimum (exploratory) | Δ loss, validation choice vs 0.6 | 95% CI |',
'|---|---:|---:|---:|---:|---:|---|']
for name,g in r['segments'].items():
 if name=='all' or name.startswith('competition:'):continue
 choice=str(g.get('validation_selected_weight',.6));best=min(g['grid'],key=lambda w:g['grid'][w]['log_loss'])
 ci=(g.get('validation_selected_log_loss_ci') or {}).get('ci95')
 val=g['grid'][choice]['delta_log_loss_vs_0.6'] if choice in g['grid'] else None
 lines.append(f'| {name} | {g.get("validation_n",0):,} | {g["n"]:,} | {choice} | {best} | {val:+.6f} | '+(f'{ci[0]:+.6f} to {ci[1]:+.6f}' if ci else 'insufficient blocks')+' |')
lines+=['',
'Large-divergence matches are especially sparse and club-clustered. Apparent subgroup optima are hypothesis generators, not deployable coefficients. Relegated-team and early-season effects need prospective or independently held-out confirmation; do not build conditional production weights from these tables.','',
'## Competition breakdown','',
'All competition grids and calibration counts are in `results.json`. The table below shows the largest cohorts; test-minimum values are exploratory.','',
'| Competition | Test n | Test minimum | Loss at 0.6 | Test minimum loss |','|---|---:|---:|---:|---:|']
comps=sorted(((k,v) for k,v in r['segments'].items() if k.startswith('competition:')),key=lambda kv:-kv[1]['n'])
for key,g in comps[:15]:
 cid=key.split(':')[1];name=competitions.get(cid,{}).get('name',cid)
 best=min(g['grid'],key=lambda w:g['grid'][w]['log_loss'])
 lines.append(f'| {name} ({cid}) | {g["n"]:,} | {best} | {g["grid"]["0.6"]["log_loss"]:.6f} | {g["grid"][best]["log_loss"]:.6f} |')
lines+=['','## Rapid improvers and decliners: descriptive checks only','',
'These examples are selected mechanically by maximum absolute Current-minus-Baseline divergence per team, not by whether a weight predicts their results well. No coefficient was tuned around them. A historical rank divergence is a model signal, not a causal claim about the club.','',
'| Direction | Team | Fixture | Date | Current | Baseline | Difference |','|---|---|---:|---|---:|---:|---:|']
for direction,examples in r['descriptive_divergence_examples'].items():
 for x in examples:
  lines.append(f'| {direction} | {team_names.get(str(x["team_id"]),str(x["team_id"]))} | {x["fixture_id"]} | {str(x["kickoff"])[:10]} | {x["current"]:.1f} | {x["baseline"]:.1f} | {x["divergence"]:+.1f} |')
lines+=['','## Sensitivity and missing comparisons','',
f'An exact-compatibility sensitivity required stored predictions to match rebuilt historical ranks at 0.6/0.4 and today’s probability formula. It retained only {s["validation_n"]:,} validation and {s["test_n"]:,} test fixtures, excluding 59,212 mismatched rows. It selected 0.7 on validation, but 0.7 worsened held-out log loss by {s["test"]["0.7"]["delta_log_loss_vs_0.6"]:.6f}; its apparent test optimum 0.4 improved by only {-s["test"]["0.4"]["delta_log_loss_vs_0.6"]:.6f}, with an interval crossing zero. This tiny, selected cohort is not representative. Results are retained in `compatible_results.json`, not substituted for the broad analysis.',
'',
'**Bookmaker paired sample: n=0.** There are no immutable timestamped odds observations yet. Current/latest odds cannot establish what was available on the same historical fixtures at prediction time; no market superiority claim is possible.',
'',
'## Review recommendation','',
'Keep 0.6 Current / 0.4 Baseline near kickoff. Keep the horizon curve and all other production components unchanged. Accumulate prospective snapshots and timestamped prices, then repeat a preregistered paired evaluation. Treat any relegation/early-season conditional weighting as a separate hypothesis requiring validation and adequate independent weeks, not a discovered production rule.',
'',
'## Reproduction','',
'`run.py` contains the grid, date boundaries, cohort definitions, metrics and paired uncertainty calculation. `results.json` contains all calibration bucket sample sizes, subgroup grids, run/source digest and frozen-input fingerprint. The input extract is stored locally at `.cache/strength_weight_inputs.json.gz` (ignored by Git; no credentials).',
'',
'```bash',
'/tmp/thecornerfc-experiment-venv/bin/python experiments/strength_weight/run.py --input-cache .cache/strength_weight_inputs.json.gz',
'/tmp/thecornerfc-experiment-venv/bin/python experiments/strength_weight/run.py --input-cache .cache/strength_weight_inputs.json.gz --strict-compatible --output experiments/strength_weight/compatible_results.json',
'python experiments/strength_weight/write_report.py','```','',
'No database writes, API requests or production formula edits were made. Tests verify the counterfactual against the full unchanged prediction formula.']
(root/'REPORT.md').write_text('\n'.join(lines)+'\n')
