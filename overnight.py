#!/usr/bin/python3
# Credit to Trevor Thackston for most of script
# See https://github.com/alpacahq/alpaca-trade-api-python/blob/master/examples/overnight_hold.py
import alpaca_trade_api as tradeapi
import pandas as pd
import statistics
import sys
import time
from datetime import datetime, timedelta
from pytz import timezone

''' Change these out with your private keys '''
base_url = 'https://paper-api.alpaca.markets'
api_key_id = 'XXXXXXXXXXXXXXXXXXXX'
api_secret = 'XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX'

stocks_to_hold = 50
max_stock_price = 50
min_stock_price = 20

# API datetimes will match this format. (-04:00 represents the market's TZ.)
api_time_format = '%Y-%m-%dT%H:%M:%S.%f-04:00'

# Rate stocks based on the volume's deviation from the previous 5 days and
# momentum. Returns a dataframe mapping stock symbols to ratings and prices.

def get_ratings(symbols, algo_time):
    assets = api.list_assets()
    assets = [asset for asset in assets if asset.tradable ]
    ratings = pd.DataFrame(columns=['symbol', 'rating', 'price'])
    index = 0
    batch_size = 200 # The maximum number of stocks to request data for
    window_size = 5 # The number of days of data to consider
    formatted_time = None
    if algo_time is not None:
        # Convert the time to something compatable with the Alpaca API.
        formatted_time = algo_time.date().strftime(api_time_format)
    while index < len(assets):
        symbol_batch = [
            asset.symbol for asset in assets[index:index+batch_size]
        ]
        # Retrieve data for this batch of symbols.
        barset = api.get_barset(
            symbols=symbol_batch,
            timeframe='day',
            limit=window_size,
            end=formatted_time
        )

        for symbol in symbol_batch:
            bars = barset[symbol]
            # A bar looks like:
            # Bar({'c': 12.99, 'h': 13.1, 'l': 12.86, 'o': 13.02, 't': 1587441600, 'v': 27136})
            if len(bars) == window_size:
                # Make sure we aren't missing the most recent data.
                latest_bar = bars[-1].t.to_pydatetime().astimezone(
                    timezone('EST')
                )
                
                if algo_time == None:
                    gap_from_present = (datetime.now(timezone('US/Eastern')) - latest_bar)
                else:
                    gap_from_present = algo_time - latest_bar
                if gap_from_present.days > 1:
                    continue

                # Now, if the stock is within our target range, rate it.
                price = bars[-1].c
                if price <= max_stock_price and price >= min_stock_price:
                    price_change = price - bars[0].c
                    # Calculate standard deviation of previous volumes
                    past_volumes = [bar.v for bar in bars[:-1]]
                    volume_stdev = statistics.stdev(past_volumes)
                    if volume_stdev == 0:
                        # The data for the stock might be low quality.
                        continue
                    # Then, compare it to the change in volume since yesterday.
                    volume_change = bars[-1].v - bars[-2].v
                    volume_factor = volume_change / volume_stdev
                    # Rating = Number of volume standard deviations * momentum.
                    # This is price change over 5 days / price beginning of window 
                    # Times the change in volume since yesterday / standard dev of window
                    rating = price_change/bars[0].c * volume_factor
                    # Weight it a little more with the price change
                    rating = 0.5 * rating + 0.5 * price_change

                    if rating > 0:
                        ratings = ratings.append({
                            'symbol': symbol,
                            'rating': price_change/bars[0].c * volume_factor,
                            'price': price
                        }, ignore_index=True)
        index += 200
    ratings = ratings.sort_values('rating', ascending=False)
    ratings = ratings.reset_index(drop=True)
    return ratings[:stocks_to_hold]


def get_shares_to_buy(ratings_df, portfolio):
    total_rating = ratings_df['rating'].sum()
    shares = {}
    prices = {}
    for _, row in ratings_df.iterrows():
        shares[row['symbol']] = int(
            row['rating'] / total_rating * portfolio / row['price']
        )
        prices[row['symbol']] = int(row['price'] * 1.5) # This is used for our limit price. To prevent from skyrocketing prices and large losses
    return shares, prices


# Returns a string version of a timestamp compatible with the Alpaca API.
def api_format(dt):
    return dt.strftime(api_time_format)

def backtest(api, days_to_test, portfolio_amount):
    # This is the collection of stocks that will be used for backtesting.
    assets = api.list_assets()
    # Note: for longer testing windows, this should be replaced with a list
    # of symbols that were active during the time period you are testing.
    symbols = [asset.symbol for asset in assets]

    now = datetime.now(timezone('EST'))
    beginning = now - timedelta(days=days_to_test)

    # The calendars API will let us skip over market holidays and handle early
    # market closures during our backtesting window.
    calendars = api.get_calendar(
        start=beginning.strftime("%Y-%m-%d"),
        end=now.strftime("%Y-%m-%d")
    )
    shares = {}
    cal_index = 0
    for calendar in calendars:
        # See how much we got back by holding the last day's picks overnight
        portfolio_amount += get_value_of_assets(api, shares, calendar.date)
        print('Portfolio value on {}: ${:0.2f}'.format(calendar.date.strftime(
            '%Y-%m-%d'), portfolio_amount)
        )

        if cal_index == len(calendars) - 1:
            # We've reached the end of the backtesting window.
            break

        # Get the ratings for a particular day
        ratings = get_ratings(symbols, timezone('EST').localize(calendar.date))
        shares, prices = get_shares_to_buy(ratings, portfolio_amount)
        v=list(shares.values())
        k=list(shares.keys())
        symbol = k[v.index(max(v))]
        
        for _, row in ratings.iterrows():
            if row['symbol'] == symbol:
            # "Buy" our shares on that day and subtract the cost.
                shares_to_buy = shares[row['symbol']]
                cost = row['price'] * shares_to_buy
                portfolio_amount -= cost
        cal_index += 1
    # Print market (S&P500) return for the time period
    sp500_bars = api.get_barset(
        symbols='SPY',
        timeframe='day',
        start=api_format(calendars[0].date),
        end=api_format(calendars[-1].date)
    )['SPY']
    sp500_change = (sp500_bars[-1].c - sp500_bars[0].c) / sp500_bars[0].c
    print('S&P 500 change during backtesting window: {:.4f}%'.format(
        sp500_change*100)
    )

    return portfolio_amount


# Used while backtesting to find out how much our portfolio would have been
# worth the day after we bought it.
def get_value_of_assets(api, shares_bought, on_date):
    if len(shares_bought.keys()) == 0:
        return 0

    total_value = 0
    formatted_date = api_format(on_date)
    barset = api.get_barset(
        symbols=shares_bought.keys(),
        timeframe='day',
        limit=1,
        end=formatted_date
    )
    for symbol in shares_bought:
        total_value += shares_bought[symbol] * barset[symbol][0].o
    return total_value


def run_live(api):
    cycle = 0 # Only used to print a "waiting" message every few minutes.

    # See if we've already bought or sold positions today. If so, we don't want to do it again.
    # Useful in case the script is restarted during market hours.
    bought_today = False
    sold_today = False
    try:
        # The max stocks_to_hold is 50, so we shouldn't see more than 400
        # orders on a given day.
        orders = api.list_orders(
            after=api_format(datetime.today() - timedelta(hours=12)),
            limit=400,
            status='all'
        )
        for order in orders:
            if order.side == 'sell':
                sold_today = True
            elif order.side == 'buy':
                bought_today = True    
    except:
        # We don't have any orders, so we've obviously not done anything today.
        pass

    while True:
        # We'll wait until the market's open to do anything.
        clock = ''
        while clock == '':
            try:
                clock = api.get_clock()
                break
            except:
                print("Connection refused by the server..")
                print("Sleeping for 15 seconds")
                time.sleep(15)
                continue
        if clock.is_open:
            # If i have not sold in last 12 hours, then I can liquidate.
            # If i have, then we forget about it and move on
            # Restarting script purposes
            if not sold_today:
                print('Liquidating positions.')
                api.close_all_positions()
            else:
                sold_today = False

            while True:
                clock = ''
                while clock == '':
                    try:
                        clock = api.get_clock()
                        break
                    except:
                        print("Connection refused by the server..")
                        print("Sleeping for 15 seconds")
                        time.sleep(15)
                        continue
                time_until_close = clock.next_close - clock.timestamp
                if time_until_close.seconds <= 120 and not bought_today:
                    print('Buying positions...')
                    portfolio_cash = float(api.get_account().cash)
                    ratings = get_ratings(
                        api, None
                    )
                    shares_to_buy, prices = get_shares_to_buy(ratings, portfolio_cash)
                    v=list(shares_to_buy.values())
                    k=list(shares_to_buy.keys())
                    symbol = k[v.index(max(v))]
                    
                    #for symbol in shares_to_buy:
                        #if shares_to_buy[symbol] > 0:    
                    try:    
                        api.submit_order(
                            symbol=symbol,
                            qty=shares_to_buy[symbol],
                            side='buy',
                            type='limit',
                            time_in_force='day',
                            limit_price=prices[symbol]
                        )
                    except:
                        print("Failed to buy ", shares_to_buy[symbol], " shares of stock ", symbol, " at price ", prices[symbol])
                    print('Positions bought.')
                    time.sleep(150)
                    break
                time.sleep(30)
        else:
            bought_today = False
            sold_today = False
            if cycle % 60 == 0:
                print("Waiting for next market day...")
            time.sleep(30)
            cycle += 1


if __name__ == '__main__':
    api = tradeapi.REST(
        base_url=base_url,
        key_id=api_key_id,
        secret_key=api_secret
    )

    if len(sys.argv) < 2:
        print('Error: please specify a command; either "run" or "backtest <cash balance> <number of days to test>".')
    else:
        if sys.argv[1] == 'backtest':
            # Run a backtesting session using the provided parameters
            start_value = float(sys.argv[2])
            testing_days = int(sys.argv[3])
            portfolio_value = backtest(api, testing_days, start_value)
            portfolio_change = (portfolio_value - start_value) / start_value
            print('Portfolio change: {:.4f}%'.format(portfolio_change*100))
        elif sys.argv[1] == 'run':
            run_live(api)
        else:
            print('Error: Unrecognized command ' + sys.argv[1])
