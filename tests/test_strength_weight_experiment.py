import importlib.util
from pathlib import Path
import unittest
from thecornerfc import predictions

spec=importlib.util.spec_from_file_location('strength_experiment',Path(__file__).resolve().parents[1]/'experiments/strength_weight/run.py')
experiment=importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


class StrengthWeightTests(unittest.TestCase):
    def test_counterfactual_matches_full_model_with_fixed_other_inputs(self):
        params=dict(home_records=[(2.,1.),(1.,0.)],away_records=[(1.,2.)],lg_home=1.5,lg_away=1.1,
                    league_id=39,home_missing=.3,away_missing=.1,sides=(.1,.2,5.,3.,1.5,1.1),
                    lines=([50.,60.,65.,55.],[52.,55.,62.,60.]))
        hc,hl,ac,al=1000.,900.,920.,950.
        baseline=predictions.predict_match(.6*hc+.4*hl,.6*ac+.4*al,**params)
        row=dict(h_cur=hc,h_lt=hl,a_cur=ac,a_lt=al,home_rank=.6*hc+.4*hl,away_rank=.6*ac+.4*al,
                 exp_diff=baseline[0],home_xg=baseline[1],away_xg=baseline[2])
        for w in experiment.WEIGHTS:
            expected=predictions.predict_match(w*hc+(1-w)*hl,w*ac+(1-w)*al,**params)
            probs,margin=experiment.counterfactual(row,w)
            for a,b in zip(probs,expected[3:6]):
                self.assertAlmostEqual(a,b,places=12)
            self.assertAlmostEqual(margin,expected[0],places=12)
        self.assertEqual(predictions.MATCH_RANK_NOW_TODAY,.6)

    def test_uncertainty_requires_multiple_temporal_blocks(self):
        row={'week':'2026-01','scores':{'0.6':{'loss':1.},'0.4':{'loss':.9}}}
        self.assertIsNone(experiment.paired_ci([row],.4)['ci95'])
