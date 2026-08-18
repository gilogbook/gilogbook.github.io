"""모든 시장 어댑터가 반환해야 하는 정규화 스키마.

ticker, name, price, market_cap, ev, ebit,
net_working_capital, net_fixed_assets,
sector, is_financial, fiscal_year_end, data_as_of

전략은 이 스키마만 보고 동작한다.
전략 코드에 국가 분기가 들어가면 잘못 만든 것이다.
"""
