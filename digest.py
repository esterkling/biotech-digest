from src.edgar import edgar_private_price_analysis

ua = os.environ["SEC_USER_AGENT"]
analysis = edgar_private_price_analysis("GENB", ua, ipo_low=15, ipo_high=17, ipo_final=16)
