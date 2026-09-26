import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from thecornerfc import evaluation as e


class EvaluationTests(unittest.TestCase):
    def test_multiclass_known_values(self):
        result=e.probability_metrics([[.7,.2,.1],[.2,.5,.3]],[0,2])
        self.assertEqual(result['n'],2)
        self.assertEqual(result['accuracy'],.5)
        self.assertAlmostEqual(result['log_loss'],(-math.log(.7)-math.log(.3))/2)
        self.assertAlmostEqual(result['brier'],(.14+.78)/2)
        self.assertEqual(sum(b['n'] for b in result['calibration']['0'].values()),2)

    def test_binary_brier_and_calibration(self):
        result=e.binary_metrics([.8,.4],[1,0])
        self.assertAlmostEqual(result['brier'],.1)
        self.assertEqual(result['calibration']['0.8-0.9']['observed_rate'],1)
        self.assertEqual(e.binary_metrics([],[])['n'],0)
        self.assertIsNone(e.binary_metrics([],[])['log_loss'])
        with self.assertRaises(ValueError):
            e.binary_metrics([float('nan')],[1])

    def test_market_comparison_uses_identical_subset(self):
        rows=[dict(probabilities=[.7,.2,.1],outcome=0,league_id=39,model_version_id='v',
                   likely_score='2-0',actual_score='2-0',market=[.5,.3,.2]),
              dict(probabilities=[.2,.5,.3],outcome=2,league_id=40,model_version_id='v',
                   likely_score=None,actual_score='0-1',market=None)]
        report=e.matches_report(rows)
        self.assertEqual(report['overall']['n'],2)
        self.assertEqual(report['paired_market']['model']['n'],1)
        self.assertEqual(report['paired_market']['market']['n'],1)
        self.assertEqual(report['overall']['exact_score'],{'n':1,'accuracy':1.})

    def test_lineup_starters_roles_and_absent_probabilities(self):
        predicted=[dict(player=p,predicted_starter=True,role='CM',line='MID',start_probability=None) for p in range(11)]
        actual=[dict(player=p,starter=True,role='CM',position='M') for p in range(1,12)]
        result=e.lineup_metrics([{'players':predicted,'official':actual}])
        self.assertEqual(result['correct_starters_mean'],10)
        self.assertAlmostEqual(result['correct_starters_out_of_11'],10/11)
        self.assertEqual(result['false_positives'],1)
        self.assertEqual(result['false_negatives'],1)
        self.assertEqual(result['role_accuracy'],{'n':10,'accuracy':1.})
        self.assertEqual(result['start_probability_metrics']['n'],0)

    def test_betting_voids_missing_clv_and_unsettled(self):
        base=dict(stake_units=1,odds_taken=3,model_probability=.6,fair_probability=.5,
                  price_clv=None,probability_movement=None)
        rows=[dict(base,result='win',profit_units=2),dict(base,result='loss',profit_units=-1),
              dict(base,result='void',profit_units=0),dict(base,result=None,profit_units=None)]
        result=e.betting_metrics(rows)
        self.assertEqual(result['n'],4)
        self.assertEqual(result['settled_n'],3)
        self.assertAlmostEqual(result['roi'],1/3)
        self.assertEqual(result['model']['n'],2)
        self.assertEqual(result['price_clv']['n'],0)
        self.assertEqual(e.edge_bucket(1.2),'0.20+')

    def test_reconstruction_is_explicit_and_dates_validated(self):
        args=SimpleNamespace(as_of=None,start=None,end=None,hours_before=0,domain='lineups',source='reconstruction')
        with self.assertRaises(ValueError):
            e.prepare(args)
        args.domain='matches'
        self.assertEqual(e.prepare(args).source,'reconstruction')

    def test_cutoffs_in_snapshot_queries(self):
        args=e.prepare(SimpleNamespace(as_of=None,start=None,end=None,hours_before=24,domain='matches',source='prospective'))
        with patch('thecornerfc.evaluation.query',return_value=[]) as query:
            rows,coverage=e.load_matches(None,args)
            sql,params=query.call_args.args[1:]
            self.assertIn('s.created_at<=s.effective_at',sql)
            self.assertEqual(sql.count('%s'),len(params))
            self.assertEqual(params[-2:],[86400,86400])
        with patch('thecornerfc.evaluation.query',return_value=[]) as query:
            e.load_lineups(None,args)
            sql,params=query.call_args.args[1:]
            self.assertIn('min(first_xi.captured_at)',sql)
            self.assertEqual(sql.count('%s'),len(params))
