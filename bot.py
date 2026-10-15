import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# Fetch credentials securely from environment variables
API_KEY = os.getenv('ALPACA_API_KEY')
SECRET_KEY = os.getenv('ALPACA_SECRET_KEY')

# Initialize the Alpaca Trading Client (Set paper=True for sandbox testing)
client = TradingClient(api_key=API_KEY, secret_key=SECRET_KEY, paper=True)

def run_strategy():
    account = client.get_account()
    print(f"Current Buying Power: ${account.buying_power}")
    
    if float(account.buying_power) > 500:
        # Prepare market order request
        order_data = MarketOrderRequest(
            symbol="SPY",
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        # Submit the order
        order = client.submit_order(order_data=order_data)
        print(f"Order submitted successfully: {order.id}")
    else:
        print("Insufficient buying power or condition not met.")

if __name__ == "__main__":
    run_strategy()
