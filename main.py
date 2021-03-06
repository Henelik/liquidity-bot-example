import asyncio
import yaml
import sys
import click
import logging as log
from qtrade_client.api import QtradeAPI

from market_data_collector import MarketDataCollector
from orderbook_manager import OrderbookManager
from vol_bot import VolBot


@click.group()
@click.option('--config', '-c', default="config.yml", type=click.File())
@click.option('--endpoint', '-e', default="https://api.qtrade.io", help='qtrade backend endpoint')
@click.option('--keyfile', '-f', default="lpbot_hmac.txt", help='a file with the hmac key', type=click.File('r'))
@click.option('--verbose', '-v', default=False, is_flag=True)
@click.pass_context
def cli(ctx, config, endpoint, keyfile, verbose):
    log_level = "DEBUG" if verbose is True else "INFO"

    root = log.getLogger()
    root.setLevel(log_level)
    handler = log.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    formatter = log.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)

    api = QtradeAPI(endpoint, key=keyfile.read().strip())
    config = yaml.load(config)

    ctx.obj['mdc'] = MarketDataCollector(config['market_data_collector'])
    ctx.obj['obm'] = OrderbookManager(
        api, config['orderbook_manager'])
    #ctx.obj['vol'] = VolBot(config, api)


@cli.command()
@click.pass_context
def run(ctx):
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(ctx.obj['obm'].monitor())
        loop.create_task(ctx.obj['mdc'].daemon())
        #loop.create_task(ctx.obj['vol'].run())
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Closing Loop")
        loop.close()


@cli.command()
@click.pass_context
def mdc(ctx):
    loop = asyncio.get_event_loop()
    loop.create_task(ctx.obj['mdc'].daemon())
    loop.run_forever()


@cli.command()
@click.pass_context
def obm(ctx):
    loop = asyncio.get_event_loop()
    loop.create_task(ctx.obj['obm'].monitor())
    loop.run_forever()


@cli.command()
@click.pass_context
def vol(ctx):
    loop = asyncio.get_event_loop()
    loop.create_task(ctx.obj['vol'].run())
    loop.run_forever()


@cli.command()
@click.pass_context
def balances_test(ctx):
    print(ctx.obj['obm'].api.balances_merged())


@cli.command()
@click.pass_context
def compute_allocations_test(ctx):
    print(ctx.obj['obm'].compute_allocations())


@cli.command()
@click.pass_context
def allocate_orders_test(ctx):
    allocs = ctx.obj['obm'].compute_allocations()
    a = allocs.popitem()[1]
    print(ctx.obj['obm'].allocate_orders(a[1], a[0]))


@cli.command()
@click.pass_context
def price_orders_test(ctx):
    allocs = ctx.obj['obm'].compute_allocations()
    m, a = allocs.popitem()
    print(ctx.obj['obm'].price_orders(
        ctx.obj['obm'].allocate_orders(a[0], a[1], m), 0.0000033, 0.0000032))


@cli.command()
@click.pass_context
def update_orders_test(ctx):
    ctx.obj['obm'].update_orders()


@cli.command()
@click.pass_context
def cancel_all(ctx):
    ctx.obj['obm'].api.cancel_all_orders()


@cli.command()
@click.pass_context
def rebalance_test(ctx):
    ctx.obj['mdc'].update_tickers()
    ctx.obj['mdc'].update_midpoints()
    print(ctx.obj['obm'].generate_orders(force_rebalance=False))


@cli.command()
@click.pass_context
def estimate_account_value(ctx):
    ctx.obj['mdc'].update_tickers()
    ctx.obj['mdc'].update_midpoints()
    print(ctx.obj['obm'].estimate_account_value())


@cli.command()
@click.pass_context
def estimate_account_gain(ctx):
    ctx.obj['mdc'].update_tickers()
    ctx.obj['mdc'].update_midpoints()
    btc_val, usd_val = ctx.obj['obm'].estimate_account_value()
    print(ctx.obj['obm'].estimate_account_gain(btc_val))


@cli.command()
@click.pass_context
def trade_tracking_test(ctx):
    print(ctx.obj['obm'].boot_trades())
    print(ctx.obj['obm'].check_for_trades())



if __name__ == "__main__":
    cli(obj={})
