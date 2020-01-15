from typing import Union

import asyncio
import logging
import heapq
from decimal import Decimal

from data_classes import ExchangeDatastore
from qtrade_client.api import QtradeAPI, APIException

from pprint import pprint, pformat

COIN = Decimal('.00000001')
PERC = Decimal('.01')

log = logging.getLogger('obm')


class MarketConfig(dict):

    def __init__(self, market_string, config, default={}):
        self.market_string = market_string
        self.update(default)
        self.update(config)


class OrderbookManager:

    def __init__(self, api, config):
        self.config = config
        self.api = api
        self.prev_alloc_profile = None
        self.market_configs = {
            ms: MarketConfig(ms, mkt, default=config['markets'].get('default'))
            for ms, mkt in config['markets'].items()
            if ms != 'default'}

    def compute_allocations(self):
        """ Given our allocation % targets and our current balances, figure out
        how much market and base currency we would _ideally_ be
        allocating to each market
        return {
            "DOGE_BTC": [1200, 0.0012],
        }
        """
        balances = {c: Decimal(b)
                    for c, b in self.api.balances_merged().items()}
        balances.update({c: 0 for c in self.config[
                        'currency_reserves'] if c not in balances.keys()})
        reserve_config = self.config['currency_reserves']
        allocs = {}
        for market_string, market_alloc in self.market_configs.items():
            market = self.api.markets[market_string]

            def allocate_coin(coin):
                """ Factor in allocation precentage and reserve amount to
                determine how much (base|market)-currency we're going to
                allocate to orders on this particular market. """
                reserve = Decimal(reserve_config[coin])
                alloc_perc = Decimal(market_alloc[coin])

                post_reserve = balances[coin] - reserve
                return max(post_reserve * alloc_perc, 0)

            market_amount = allocate_coin(market['market_currency']['code'])
            base_amount = allocate_coin(market['base_currency']['code'])

            # TODO: At some point COIN will need to be based off base currency
            # precision. Not needed until we have ETH base markets really
            allocs[market_string] = (market_amount, base_amount)
        return allocs

    def allocate_orders(self, market_alloc, base_alloc, market_string):
        """ Given some amount of base and market currency determine how we'll
        allocate orders. Returns a tuple of (slippage_ratio, currency_allocation)
        return {
            "buy_limit": [
                (0.01, 0.00001256),
            ],
            "sell_limit": [
                (0.01, 1250),
            ]
        }
        """
        buy_allocs = []
        sell_allocs = []
        mc = self.market_configs[market_string]
        for slip, ratio in mc['intervals']['sell_limit'].items():
            ratio = Decimal(ratio)
            amount = (market_alloc * ratio).quantize(COIN)
            sell_allocs.append((slip, amount))
        for slip, ratio in mc['intervals']['buy_limit'].items():
            ratio = Decimal(ratio)
            value = (base_alloc * ratio).quantize(COIN)
            buy_allocs.append((slip, value))
        return {'buy_limit': buy_allocs, 'sell_limit': sell_allocs}

    def price_orders(self, orders, bid, ask):
        """
        return {
            "buy_limit": [
                (0.00000033, 0.00001256),
            ],
            "sell_limit": [
                (0.00000034, 1250),
            ]
        } """
        priced_sell_orders = []
        priced_buy_orders = []
        bid = Decimal(bid)
        ask = Decimal(ask)
        for slip, amount in orders['sell_limit']:
            slip = Decimal(slip)
            price = (ask + (ask * slip)).quantize(COIN)
            priced_sell_orders.append((price, amount))
        for slip, value in orders['buy_limit']:
            slip = Decimal(slip)
            price = (bid - (bid * slip)).quantize(COIN)
            priced_buy_orders.append((price, value))
        return {'buy_limit': priced_buy_orders, 'sell_limit': priced_sell_orders}

    def rebalance_orders(self, allocation_profile, orders, force=False):
        if self.check_for_rebalance(allocation_profile) is False and force is False:
            return

        if self.config['dry_run_mode']:
            log.warning(
                "You are in dry run mode! Orders will not be cancelled or placed!")
            #pprint(allocation_profile)
            return

        self.api.cancel_all_orders()

        for market_string, profile in allocation_profile.items():
            for price, value in profile['buy_limit']:
                self.place_order('buy_limit', market_string, price, value)
            for price, amount in profile['sell_limit']:
                self.place_order('sell_limit', market_string, price, amount)
        self.prev_alloc_profile = allocation_profile

    def place_order(self, order_type, market_string, price, quantity):
        if quantity <= 0:
            return
        log.info("Placing %s on %s market for %s at %s",
                 order_type, market_string, quantity, price)
        if order_type == 'buy_limit':
            value = quantity
            amount = None
        elif order_type == 'sell_limit':
            value = None
            amount = quantity
        try:
            self.api.order(order_type, price, market_string=market_string,
                           value=value, amount=amount, prevent_taker=False)
        except APIException as e:
            if e.code == 400:
                log.warning("Caught API error!")
            else:
                raise e

    def check_for_rebalance(self, allocation_profile):
        if self.prev_alloc_profile is None:
            log.info("Rebalance! No previous rebalance data!")
            return True

        for market, profile in allocation_profile.items():
            prev_profile = self.prev_alloc_profile[market]
            for t in ('buy_limit', 'sell_limit'):
                for n, o in zip(profile[t], prev_profile[t]):
                    price_diff = (n[0] - o[0]) / n[0]
                    price_tol = self.config['price_tolerance']
                    if price_diff > price_tol:
                        if o[0] > price_diff:
                            log.info('Rebalance! %s %s price is %s%% higher than allotted',
                                     market, t, price_diff.quantize(PERC)*Decimal(100))
                        else:
                            log.info('Rebalance! %s %s price is %s%% lower than allotted',
                                     market, t, price_diff.quantize(PERC)*Decimal(100))
                        return True
                    if n[1] == 0:
                        continue
                    amount_diff = (n[1] - o[1]) / n[1]
                    amount_tol = self.config['amount_tolerance']
                    if amount_diff > amount_tol:
                        if o[1] > amount_diff:
                            log.info('Rebalance! %s %s amount is %s%% higher than allotted',
                                     market, t, amount_diff.quantize(PERC)*Decimal(100))
                        else:
                            log.info('Rebalance! %s %s amount is %s%% lower than allotted',
                                     market, t, amount_diff.quantize(PERC)*Decimal(100))
                        return True

        balances = self.api.balances()
        for coin, reserve in self.config['currency_reserves'].items():
            balance_usd = self.coin_to_usd(coin, balances.get(coin, 0))
            reserve_usd = self.coin_to_usd(coin, reserve)
            thresh = Decimal(self.config['reserve_thresh_usd'])
            if balance_usd > reserve_usd + thresh:
                log.info(f"Rebalance! {coin} balance_usd {balance_usd} > reserve {reserve} + thresh {thresh}.")
                return True
            if balance_usd < reserve_usd - thresh:
                log.info(f"Rebalance! {coin} balance_usd {balance_usd} < reserve {reserve} - thresh {thresh}.")
                return True
        return False

    def get_orders(self):
        orders = self.api.get("/v1/user/orders")["orders"]

        log.debug("Updating orders...")
        sorted_orders = {}
        for o in orders:
            if o['open']:
                mi = self.api.get(
                    "/v1/market/" + str(o['market_id']))['market']
                o['price'] = Decimal(o['price'])
                o['market_amount_remaining'] = Decimal(
                    o['market_amount_remaining'])
                o['base_amount'] = o['price'] * o['market_amount_remaining']
                market = mi['market_currency'] + '_' + mi['base_currency']
                sorted_orders.setdefault(market, {'buy': [], 'sell': []})
                if o["order_type"] == "sell_limit":
                    sorted_orders[market]['sell'].append(o)
                elif o["order_type"] == "buy_limit":
                    sorted_orders[market]['buy'].append(o)
        log.debug("Active buy orders: %s", sorted_orders)

        log.info("%s active buy orders", sum(
            [len(market['buy']) for market in sorted_orders.values()]))
        log.info("%s active sell orders", sum(
            [len(market['sell']) for market in sorted_orders.values()]))
        return sorted_orders

    def generate_orders(self, force_rebalance=False):
        allocs = self.compute_allocations()
        allocation_profile = {}
        for market, (market_amount, base_amount) in allocs.items():
            if market in ExchangeDatastore.tickers['bittrex'].keys():
                bid = ExchangeDatastore.tickers['bittrex'][market]['bid']
                ask = ExchangeDatastore.tickers['bittrex'][market]['ask']
            elif market in ExchangeDatastore.tickers['ccxt'].keys():
                bid = ExchangeDatastore.tickers['ccxt'][market]['bid']
                ask = ExchangeDatastore.tickers['ccxt'][market]['ask']
            else:
                log.warning(f"Can't get bid/ask price for {market} to generate orders!")
                continue
            log.info("Generating %s orders with bid %s and ask %s",
                     market, bid, ask)
            allocation_profile[market] = self.price_orders(
                self.allocate_orders(market_amount, base_amount, market), bid, ask)
        self.rebalance_orders(allocation_profile,
                              self.get_orders(), force=force_rebalance)

    def estimate_account_value(self):
        # convert all coin values to BTC using the Bittrex bid price
        # then convert to USD
        total_bal = 0
        bals = self.api.balances_merged()
        for coin, bal in bals.items():
            if coin == "BTC":
                total_bal += Decimal(bal)
            else:
                total_bal += self.coin_to_btc(coin, bal)
        return total_bal, self.btc_to_usd(total_bal).quantize(PERC)

    def estimate_account_gain(self, btc_bal):
        cost_basis = Decimal(self.config['cost_basis_btc'])
        gain = (btc_bal - cost_basis).quantize(COIN)
        return gain, self.btc_to_usd(gain).quantize(PERC)

    def coin_to_btc(self, coin, amt):
        exchanges = ['bittrex', 'ccxt', 'qtrade']
        for e in exchanges:
            try:
                bid = ExchangeDatastore.tickers[e][coin + '_BTC']['bid']
                return (Decimal(amt) * Decimal(bid)).quantize(COIN)
            except KeyError:
                pass
        log.warning("Can't get bid price for %s for price estimation", coin)
        return 0

    def btc_to_usd(self, amt):
        btc_price = Decimal(self.api.get('/v1/currency/BTC')
                            ['currency']['config']['price'])
        return Decimal(amt) * btc_price

    def coin_to_usd(self, coin: str, amt: Union[Decimal, float]) -> Decimal:
        if coin == "BTC":
            return self.btc_to_usd(amt)
        return self.btc_to_usd(self.coin_to_btc(coin, amt)).quantize(PERC)

    def boot_trades(self):
        trades = {t['id']: t for t in self.api.get('/v1/user/trades')['trades']}
        newest_ids = heapq.nlargest(10, trades.keys())
        recent_trades = {id: trades[id] for id in newest_ids}
        self.most_recent_trade_id = max(newest_ids)
        log.info("10 most recent trades:\n%s", pformat(recent_trades))

    def check_for_trades(self):
        res = self.api.get('/v1/user/trades', newer_than=self.most_recent_trade_id)
        if res['trades'] == []:
            log.info('No new trades!')
            return
        trades = {t['id']: t for t in res['trades']}
        if self.config['dry_run_mode'] is False:
            log.info("Bot made new trades:\n%s", pformat(trades))
        self.most_recent_trade_id = max(trades.keys())

    async def monitor(self):
        # Sleep to allow data scrapers to populate
        await asyncio.sleep(2)
        log.info("Starting orderbook manager; interval period %s sec",
                 self.config['monitor_period'])
        self.boot_trades()
        while True:
            try:
                self.generate_orders()
                btc_val, usd_val = self.estimate_account_value()
                log.info("Current account value is about $%s, %s BTC",
                         usd_val, btc_val)
                btc_gain, usd_gain = self.estimate_account_gain(btc_val)
                log.info("The bot has earned $%s, %s BTC",
                         usd_gain, btc_gain)
                self.check_for_trades()
                await asyncio.sleep(self.config['monitor_period'])
            except Exception:
                log.warning("Orderbook manager loop exploded", exc_info=True)
                try:
                    # Just in case the entire program explodes, so that we don't have orders out.
                    self.api.cancel_market_orders()
                except Exception:
                    pass
