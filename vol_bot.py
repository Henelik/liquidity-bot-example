import numpy as np
import time
import logging
import asyncio

from collections import namedtuple
from decimal import Decimal

from qtrade_client.api import APIException

log = logging.getLogger('vol')


class Trade(namedtuple('Trade', ['time_until', 'curr_code', 'perc', 'side'])):
    def __str__(self):
        return "<Trade +{time_until:.2f}s {side} {curr_code} {perc:.3f}%>".format(**self._asdict())


class VolBot:
    def __init__(self, config, api):
        self.data_series = []
        self.api = api
        self.config = config['vol_bot_manager']
        self.q = self.config['default']['q']
        self.var = self.config['default']['var']
        self.amount = self.config['default']['amount']
        self.open_trade = 0

        self.btc_price = self.config.get('btc_price', 8500)
        self.fake_sleep = self.config.get('fake_sleep', False)
        # this means it actually doesn't run
        self.dry = self.config.get('dry', True)

    async def sleep(self, time):
        if self.fake_sleep:
            log.info(f"Would've slept for {time:.2f}, but fake sleep skip")
            return
        await asyncio.sleep(time)

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
        reserves = self.config['currency_reserves']
        balances = {key: balances.get(key, 0) - Decimal(reserves.get(key, 0))
                    for key in balances.keys()}

        # TODO adding traded markets from config
        markets = ['ETH', 'LTC', 'NANO', 'DOGE']
        amounts = {f'{market}_BTC': {c: (Decimal(b) * balances[c])
                                     for c, b in self.config['markets'][f'{market}_BTC'].items()}
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

        val = round(np.random.normal(loc=loc, scale=scale))
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
        n = round(self.trunc_normal_dist(loc=q, scale=var, trunc=3 * var))

        # the times are [60, 3540] to have overhead for bot operations
        # could also think about a better distribution
        trades = np.sort(np.random.uniform(60, 3540, size=n))

        # TODO - pull and create from market config
        mkt_strings = ['ETH', 'NANO', 'DOGE', 'LTC']
        int_to_cur = {(n + 1): mkt_string for n, mkt_string in enumerate(mkt_strings)}

        # this assumes we want all currencies to show up equally - could do a function of volume
        currency = [int_to_cur[np.random.randint(1, max(int_to_cur.keys()) + 1)] for _ in range(len(trades))]

        amounts = [max(.01, min(.3, abs(np.random.normal(loc=amount / 2, scale=amount / 4)))) for _ in range(len(trades))]

        buy_or_sell = np.random.choice(['buy', 'sell'], size=len(trades))

        return [Trade(trade, cur, amount, b_or_s) for trade, cur, amount, b_or_s in zip(trades, currency, amounts, buy_or_sell)]

    def check_orderbook(self, trade: Trade):
        '''
        Accepts:
        trade: the list of the potential trade

        Returns:
        Boolean: the Boolean is the NBBO is us

        TODO:
        Get the value for the 'market order' from here to avoid another API call
        Might just check one-side and just move into that one
        '''

        market = f'{trade.curr_code}_BTC'

        res = self.api.orders(open=True)
        orderbook = self.api.get(f"/v1/orderbook/{market}")

        orders = list(filter(lambda x: x['market_string'] == market, res))

        if len(orders) == 0:
            # sometimes the lp_bot cancels orders to rebalance, if this happens, we just cancel.
            return False

        if trade.side == 'buy':
            ba = min(filter(lambda x: x['order_type'] == 'sell_limit', orders), key=lambda k: k['price'])
            ba_key = min(orderbook['sell'].keys())

            if (ba['price'] == ba_key) and (ba['market_amount'] == orderbook['sell'][ba_key]):
                self.open_order = [ba['price'], ba['market_amount']]
                return True
            else:
                return False

        # maybe should be a else to avoid errors
        elif trade.side == 'sell':
            bb = max(filter(lambda x: x['order_type'] == 'buy_limit', orders), key=lambda k: k['price'])
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
        if trade.side == 'buy':

            # how much I have * percent I want to spend / price will give me amount of eth I can purchase at the price
            trade_price = self.open_order[0]
            trade_amount = amounts[f'{trade.curr_code}_BTC']['BTC'] * Decimal(trade.perc) / Decimal(trade_price)

            if (Decimal(self.open_order[1]) - trade_amount) > 0:
                pass
            else:
                trade_amount = self.open_order[1]
            return [trade_price, trade_amount]

        elif trade.side == 'sell':
            trade_price = self.open_order[0]
            trade_amount = amounts[f'{trade.curr_code}_BTC'][trade.curr_code] * Decimal(trade.perc) * Decimal(trade_price)

            if (Decimal(self.open_order[1]) - trade_amount) > 0:
                pass
            else:
                trade_amount = self.open_order[1]
            return [trade_price, trade_amount]

        else:
            return [0, 0]

    def place_order(self, order_type, market_string, price, quantity):
        if self.dry is True:
            raise Exception("Attempted to place order in dry run mode!")

        if quantity <= 0:
            log.debug("Attempted to place order for negative quantity")
            return

        log.debug("Placing %s on %s market for %s at %s",
                  order_type, market_string, quantity, price)
        if order_type == 'buy':
            value = quantity
            amount = None
        elif order_type == 'sell':
            value = None
            amount = quantity
        return self.api.order(
            order_type, price, market_string=market_string, value=value,
            amount=amount, prevent_taker=False)['data']['order']

    async def generate_series(self):
        if self.dry is True:
            log.warning(
                "You are in dry run mode for vol_bot! Orders will not be cancelled or placed!")
        else:
            log.info("Running in production mode for vol_bot! Orders _will_ be placed!")

        trades = self.generate_trades(self.q, self.var, self.amount)
        log.debug(f"Generated trades: {trades}")
        for count, trade in enumerate(trades):

            # sleep the time needed and less than 10 seconds than that. It
            # will never be zero, but might want to check
            if count == 0:
                sleep_time = trade.time_until
            else:
                sleep_time = trades[count].time_until - trades[count - 1].time_until
                if sleep_time < 10:
                    continue

            sleep_time -= 10
            log.info(f"Sleeping for {sleep_time:.2f}")
            await self.sleep(sleep_time)
            log.debug(f"Finished sleeping for {sleep_time:.2f}")

            amounts = self.compute_allocations()
            if not self.check_orderbook(trade):
                # not first on order book
                log.info('Not first on the book for %s', trade)
                continue

            price, quantity = self.price_trade(trade, amounts)
            usd_value = round(self.btc_price * float(price) * float(quantity), 2)
            # need to change string to market_string
            if self.dry is True:
                # dry run
                log.info(f"Would've exec {trade} of {quantity:.4f} {trade.curr_code} @ {price} (${usd_value})")
                continue

            log.info(f"Placing order to exec {trade} of {quantity:.4f} {trade.curr_code} @ {price} (${usd_value})")
            try:
                new_order = self.place_order(trade.side, trade.curr_code, price, round(float(quantity), 6))
            except Exception:
                log.warn("Unknown error placing order", exc_info=True)
                continue
            except APIException as e:
                log.warn(f"Failed to place order: {e}")
                continue

            # Fill or Kill - move to a different function once it works
            await asyncio.sleep(1)
            res = self.api.get(f"/v1/user/order/{new_order['id']}")
            if res['open'] == 'true':
                # Kill
                self.api.post('/v1/user/cancel_order', json={'id': new_order['id']})
            else:
                # Fill
                pass

    async def run(self):
        strt_time = time.time()
        while True:
            await self.generate_series()

        remaining_time = strt_time + 3600 - time.time()
        log.info('Remaining Time: %s', remaining_time)
        await asyncio.sleep(remaining_time)
