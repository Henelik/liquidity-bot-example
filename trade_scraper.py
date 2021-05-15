from qtrade_client.api import QtradeAPI
import json

def scrape_trades(api):
    trades = api.get('/v1/user/trades')["trades"]

    while True:
        new_trades = api.get('/v1/user/trades', newer_than=trades[-1]["id"])["trades"]

        if len(new_trades) == 0:
            break

        trades += new_trades

    return trades

if __name__ == "__main__":
    hmac = open("lpbot_hmac.txt").read().strip()

    api = QtradeAPI("https://api.qtrade.io", key=hmac)

    trades = scrape_trades(api)

    f = open("trades.json", "w")
    f.write(json.dumps(trades))
    f.close()