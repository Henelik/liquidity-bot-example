import numpy as np
import pandas as pd
import os
from qtrade_client.api import QtradeAPI, APIException
import yaml
from decimal import Decimal
import time
from datetime import datetime
import pickle
import logging
import asyncio

log = logging.getLogger('vol')

class VolBot:
    def __init__(self, config, api):
        self.data_series = [] 
        self.api = api
        self.config = config
        self.q = config['vol_bot_manager']['default']['q']
        self.var = config['vol_bot_manager']['default']['var']
        self.amount = config['vol_bot_manager']['default']['amount']
        self.open_trade = 0
        
        # this means it actually doesn't run
        self.dry = config['vol_bot_manager']['dry']
        
    def compute_allocations(self):
        '''
        Accepts:
        N/A
        
        Returns: Dictionary of Dictionaries in the form 
            {'currency'_'base-cur': {'base_cur': 'available amount to trade',
                                    'currency': 'avaialble amount to trade'}
            for all currencies
                                                      
        '''
        
        balances = self.api.balances_merged()
        balances = {key: balances[key] - Decimal(self.config['vol_bot_manager']['currency_reserves'][key]) 
                                     for key in self.config['vol_bot_manager']['currency_reserves'].keys()}
        
        # TODO adding traded markets from config
        markets = ['ETH', 'LTC', 'NANO', 'DOGE']
        amounts = {f'{market}_BTC': {c: (Decimal(b) * balances[c]) 
                                     for c, b in self.config['vol_bot_manager']['markets'][f'{market}_BTC'].items()} 
                                     for market in markets}
        return amounts
    
    def trunc_normal_dist(self, loc, scale, trunc):
        '''
        Accepts:
        loc: mean
        scale: std
        trunc: when should the func truncate

        Returns:
        val: a value from a truncated normal distribution
        '''
        
        val = round(np.random.normal(loc = loc, scale = scale))
        val = val if abs(val - loc) < trunc else ((np.random.choice([-1, 1]) * trunc) + loc)

        return val
    
    def generate_trades(self, q, var, amount):
        '''
        Accepts:
        q: expected average trades per hour
        var: expected volatility of individual trade amounts
        amount: max percentage of total available for each currency to trade

        Returns:
        trades: a list of trades in the form [tmie, currency_str, percentage]
        
        TODO:
        Inside the code
        '''
        
        # TODO create a truncated normal function
        n = round(self.trunc_normal_dist(loc = q, scale = var, trunc = 3 * var))

        # the times are [60, 3540] to have overhead for bot operations
        # could also think about a better distribution
        trades = np.sort(np.random.uniform(60, 3540, size = n))

        # TODO - pull and create from market config
        mkt_strings = ['ETH', 'NANO', 'DOGE', 'LTC']
        int_to_cur = {(n + 1): mkt_string for n, mkt_string in enumerate(mkt_strings)}

        # this assumes we want all currencies to show up equally - could do a function of volume
        currency = [int_to_cur[np.random.randint(1, max(int_to_cur.keys()) + 1)] for _ in range(len(trades))]

        amounts = [max(.01, min(.3, abs(np.random.normal(loc = amount / 2, scale = amount / 4)))) for _ in range(len(trades))]
        
        buy_or_sell = np.random.choice(['buy', 'sell'], size = len(trades))
        
        return [[trade, cur, amount, b_or_s] for trade, cur, amount, b_or_s in zip(trades, currency, amounts, buy_or_sell)]
    
    def check_orderbook(self, trade):
        '''
        Accepts:
        trade: the list of the potential trade

        Returns:
        Boolean: the Boolean is the NBBO is us
        
        TODO:
        Get the value for the 'market order' from here to avoid another API call
        Might just check one-side and just move into that one
        '''
        
        market = f'{trade[1]}_BTC'

        res = self.api.orders(open = True)
        orderbook = self.api.get(f"/v1/orderbook/{market}")

        orders = list(filter(lambda x: x['market_string'] == market, res))

        if len(orders) == 0:
            # sometimes the lp_bot cancels orders to rebalance, if this happens, we just cancel.
            return False
        
        if trade[3] == 'buy':
            ba = min(filter(lambda x: x['order_type'] == 'sell_limit', orders), key = lambda k: k['price'])
            ba_key = min(orderbook['sell'].keys())
       
            if (ba['price'] == ba_key) and (ba['market_amount'] == orderbook['sell'][ba_key]):
                self.open_order = [ba['price'], ba['market_amount']]
                return True
            else:
                return False
            
        # maybe should be a else to avoid errors
        elif trade[3] == 'sell':
            bb = max(filter(lambda x: x['order_type'] == 'buy_limit', orders), key = lambda k: k['price'])
            bb_key = max(orderbook['buy'].keys())

            if (bb['price'] == bb_key) and (bb['market_amount'] == orderbook['buy'][bb_key]):
                self.open_order = [bb['price'], bb['market_amount']]
                return True
            else:
                return False
        else:
            # TODO - add a log warning here
            return False
    
    def price_trade(self, trade, amounts):
        if trade[3] == 'buy':

            # how much I have * percent I want to spend / price will give me amount of eth I can purchase at the price
            trade_price = self.open_order[0]
            trade_amount = amounts[f'{trade[1]}_BTC']['BTC'] * Decimal(trade[2]) / Decimal(trade_price)
    
            if (Decimal(self.open_order[1]) - trade_amount) > 0:
                pass
            else:
                trade_amount = self.open_order[1]
            return [trade_price, trade_amount]
        
        elif trade[3] == 'sell':
            trade_price = self.open_order[0]
            trade_amount = amounts[f'{trade[1]}_BTC'][trade[1]] * Decimal(trade[2]) * Decimal(trade_price)

            if (Decimal(self.open_order[1]) - trade_amount) > 0:
                pass
            else:
                trade_amount = self.open_order[1]
            return [trade_price, trade_amount]

        else:
            return [0, 0]
    def place_order(self, order_type, market_string, price, quantity):
        if self.dry == True:
            print('This should not happen')
        else:
            if quantity <= 0:
                return
                                    
    #         log.info("Placing %s on %s market for %s at %s",
    #                  order_type, market_string, quantity, price)
            if order_type == 'buy':
                value = quantity
                amount = None
            elif order_type == 'sell':
                value = None
                amount = quantity
            try:
                res = self.api.order(order_type, price, market_string=market_string,
                            value=value, amount=amount, prevent_taker=False)
                return [True, res]
            except APIException as e:
                return [False, {}]
                                   
    async def run(self):
        # main event loop
        #log.info("Placing %s on %s market for %s at %s",
        #         order_type, market_string, quantity, price)
        while True:
            if self.dry == False:
                log.warning(
                    "You are in dry run mode for vol_bot! Orders will not be cancelled or placed!")
            
            strt_time = time.time()
            trades = self.generate_trades(self.q, self.var, self.amount)
            print(trades)
            for count, trade in enumerate(trades):

                # sleep the time needed and less than 10 seconds than that. It will never be zero, but might want to check
                if count == 0:
                    sleep_time = trade[0]
                else:
                    sleep_time = trades[count][0] - trades[count-1][0]
                    if sleep_time < 10:
                        continue
                log.info("Sleeping for %s", (sleep_time - 10))
                await asyncio.sleep(sleep_time - 10)
                log.info("Finished %s", (sleep_time - 10))

                amounts = self.compute_allocations()
                if self.check_orderbook(trade):
                    price, quantity = self.price_trade(trade, amounts)
                    # place_order(self, order_type, market_string, price, quantity):
                    # need to change string to market_string
                    if self.dry is False:
                        res = self.place_order(trade[3], trade[1], price, round(float(quantity), 6))
                        if res[0]:
                            # Fill or Kill - move to a different function once it works
                            order_id = res[1]['data']['order']['id']
                            await asyncio.sleep(1)
                            res = self.api.get(f"/v1/user/order/{order_id}")
                            if res['data']['order']['open'] == 'true':
                                # Kill
                                self.api.post('/v1/user/cancel_order', json={'id': order_id})
                            else:
                                # Fill
                                pass
                        else:
                            pass
                            # place order failed - probably should flesh this out
                    else:
                        # dry run
                        log.info("Would have executed %s %s %s at %s", trade[3], round(float(quantity), 6), trade[1], price)

                else:
                    # not first on order book
                    log.info('Not first on the book for %s for %s', trade[1], round(float(trade[2]), 6))
        remaining_time = strt_time + 3600 - time.time()
        log.info('Reamaining Time: %s', remaining_time)
        await asyncio.sleep(remaining_time)
