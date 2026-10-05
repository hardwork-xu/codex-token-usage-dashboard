"""Cross-model and calendar conservation; all records here are synthetic."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pricing import estimate_turn, validated_slices
from periods import summarize_periods

FIELDS = ('total', 'input', 'cachedInput', 'cacheWriteInput', 'output', 'reasoningOutput')
SETTINGS = dict(pricingMode='official', speedMode='standard', usdPerCredit='0.04', currencyPerUsd='1', subscriptionRenewalDay=9)

def counts(n):
    return dict(total=n, input=n, cachedInput=0, cacheWriteInput=0, output=0, reasoningOutput=0)

def fixture(items):
    turn = dict(id='fixture-turn', threadId='fixture-thread', model=None, serviceTier=None,
                pricingMetadataStatus='mixed', quality='complete', status='completed',
                tokens=counts(0), dailyUsage={}, undatedTokens=counts(0), usageSlices=[])
    for day, model, n in items:
        item = dict(day=day, model=model, serviceTier='standard', pricingMetadataStatus='known', tokens=counts(n))
        turn['usageSlices'].append(item)
        bucket = turn['dailyUsage'].setdefault(day, counts(0)) if day else turn['undatedTokens']
        for key in FIELDS:
            turn['tokens'][key] += item['tokens'][key]
            bucket[key] += item['tokens'][key]
    return turn

class SliceTests(unittest.TestCase):
    def test_same_turn_models_have_distinct_prices_and_conserved_totals(self):
        turn = fixture([('2026-09-27','gpt-6-astra',1000000),('2026-09-27','gpt-5.6-luna',2000000)])
        price = estimate_turn(turn, SETTINGS)
        self.assertEqual(price['amount'], '10.400000')
        self.assertEqual(price['credits'], '260.000000')
        self.assertEqual(price['status'], 'estimated')
        self.assertEqual({x['model']:x['tokens']['total'] for x in price['models']}, {'gpt-6-astra':1000000,'gpt-5.6-luna':2000000})
        period = summarize_periods([turn, deepcopy(turn)], SETTINGS, now=date(2026,9,27))['today']
        self.assertEqual(period['turnCount'], 1)
        self.assertEqual([g['turnCount'] for g in period['models']], [1,1])
        for key in FIELDS:
            self.assertEqual(sum(g['tokens'][key] for g in period['models']), period['tokens'][key])
        for key in ('amount','credits','usd'):
            self.assertEqual(sum(Decimal(g[key]) for g in period['models']), Decimal(period[key]))

    def test_top_level_astra_does_not_override_other_model_rates(self):
        turn=fixture([('2026-09-27',model,105000) for model in ('gpt-6-astra','gpt-6-sol','gpt-6-luna')])
        turn['model']='gpt-6-astra'
        tokens=dict(total=105000,input=100000,cachedInput=90000,cacheWriteInput=0,output=5000,reasoningOutput=3000)
        for item in turn['usageSlices']:item['tokens']=dict(tokens)
        turn['tokens']={key:value*3 for key,value in tokens.items()}
        turn['dailyUsage']['2026-09-27']=dict(turn['tokens'])
        price=estimate_turn(turn,SETTINGS)
        period=summarize_periods([turn],SETTINGS,now=date(2026,9,27))['today']
        for result in (price,period):
            self.assertEqual(result['credits'],'13.310000')
            self.assertEqual(result['amount'],'0.532400')
            self.assertNotEqual(result['amount'],'1.320000')
            self.assertEqual({g['model']:g['amount'] for g in result['models']},
                             {'gpt-6-astra':'0.440000','gpt-6-sol':'0.088000','gpt-6-luna':'0.004400'})
            rates={g['model']:g['standardRates'] for g in result['models']}
            self.assertEqual(rates['gpt-6-sol']['uncachedInput'],'50')
            self.assertEqual(rates['gpt-6-luna']['unit'],'credits_per_million_tokens')

    def test_unknown_fragment_does_not_erase_priced_part(self):
        turn = fixture([('2026-09-27',None,1000000),('2026-09-27','gpt-6-astra',2000000)])
        for result in (estimate_turn(turn, SETTINGS), summarize_periods([turn], SETTINGS, now=date(2026,9,27))['today']):
            self.assertEqual(result['amount'], '20.000000')
            self.assertEqual(result['unpricedTokens'],1000000)
            self.assertEqual(result['status'],'partial')
        period = summarize_periods([turn], SETTINGS, now=date(2026,9,27))['today']
        self.assertEqual(period['unpricedTurnCount'],1)

    def test_model_midnight_switch_filters_before_pricing(self):
        turn = fixture([('2026-09-26','gpt-6-astra',1000000),('2026-09-27','gpt-5.6-luna',1000000), (None,'gpt-6-astra',300)])
        periods = summarize_periods([turn], SETTINGS, now=date(2026,9,27))
        self.assertEqual(periods['today']['amount'], '0.200000')
        self.assertEqual(periods['today']['models'][0]['model'],'gpt-5.6-luna')
        self.assertEqual(periods['subscription']['amount'],'10.200000')
        self.assertEqual(periods['unassignedTokens'],300)

    def test_same_model_tiers_merge_row_but_price_separately(self):
        turn=fixture([('2026-09-27','gpt-6-astra',1000000)]*2)
        turn['usageSlices'][1]['serviceTier']='fast'
        value=estimate_turn(turn, {**SETTINGS,'speedMode':'auto'})
        self.assertEqual(value['amount'],'30.000000') # Recorded Standard 10 + recorded purchased-Credits Fast 20
        self.assertEqual(len(value['models']),1)
        self.assertEqual(value['models'][0]['turnCount'],1)

    def test_aggregate_days_before_rounding(self):
        turn=fixture([('2026-09-26','gpt-6-astra',1),('2026-09-27','gpt-6-astra',1)])
        settings={**SETTINGS,'pricingMode':'custom','ratePerMillion':'0.4'}
        self.assertEqual(estimate_turn(turn,settings)['amount'],'0.000001')
        self.assertEqual(summarize_periods([turn],settings,now=date(2026,9,27))['subscription']['amount'],'0.000001')

    def test_malformed_slices_cannot_inflate_or_misdate_usage(self):
        for change in ('sum','date','metadata'):
            turn=fixture([('2026-09-27','gpt-6-astra',100)])
            if change=='sum': turn['usageSlices'][0]['tokens']=counts(99999)
            if change=='date': turn['usageSlices'][0]['day']='2026-09-26'
            if change=='metadata': turn['usageSlices'][0]['model']='private synthetic phrase'
            price=estimate_turn(turn, SETTINGS)
            period=summarize_periods([turn], SETTINGS, now=date(2026,9,27))['today']
            for result in (price,period):
                self.assertIsNone(result['amount'])
                self.assertIsNone(result['models'][0]['model'])
                self.assertEqual(result['models'][0]['tokens']['total'],100)
            self.assertNotIn('private synthetic phrase',str(price))

    def test_new_rates_and_currency_conversion(self):
        turn=fixture([('2026-09-27','gpt-6-sol',1000000),('2026-09-27','gpt-6-luna',1000000)])
        self.assertEqual(estimate_turn(turn,SETTINGS)['amount'],'2.100000')
        self.assertEqual(estimate_turn(turn,{**SETTINGS,'currencyPerUsd':'7'})['amount'],'14.700000')

    def test_unpriceable_large_counts_remain_in_model_totals(self):
        turn=fixture([('2026-09-27','gpt-6-astra',10**31)])
        for value in (estimate_turn(turn,SETTINGS),summarize_periods([turn],SETTINGS,now=date(2026,9,27))['today']):
            self.assertEqual(value['models'][0]['tokens']['total'],10**31)
            self.assertEqual(value['unpricedTokens'],10**31)
            self.assertIsNone(value['amount'])

    def test_evidence_segments_round_once_per_model(self):
        turn=fixture([('2026-09-27','gpt-6-luna',1)]*2)
        turn['usageSlices'][1]['serviceTier']='priority'
        self.assertEqual(estimate_turn(turn,SETTINGS)['credits'],'0.000005')
        custom={**SETTINGS,'pricingMode':'custom','ratePerMillion':'0.4'}
        self.assertEqual(estimate_turn(turn,custom)['amount'],'0.000001')
        self.assertEqual(summarize_periods([turn],custom,now=date(2026,9,27))['today']['amount'],'0.000001')

    def test_input_unchanged(self):
        turn=fixture([('2026-09-27','gpt-6-astra',100)])
        before=deepcopy(turn)
        estimate_turn(turn,SETTINGS)
        summarize_periods([turn],SETTINGS,now=date(2026,9,27))
        self.assertEqual(turn,before)

if __name__=='__main__': unittest.main()
