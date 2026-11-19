"""Закрытые часы Bybit Spot и причинные признаки F13."""

import numpy as np
import pandas as pd

HOUR = pd.Timedelta(hours=1)
DAY = pd.Timedelta(days=1)
WINDOW = 30
HISTORY_DAYS = 100 + WINDOW
OHLC = ("Open", "High", "Low", "Close")
FEATURES = (
    "AlignedReturn5", "AlignedReturn20", "MomentumStrength60", "MomentumChange5",
    "Efficiency20", "FlipCount20", "VolatilityRatio", "AdverseMove20",
    "Range20", "Sigma20", "Conflict5", "Conflict20", "Side",
)
SEEDS = (11, 22, 33, 42, 55)


def validate_hourly(frame, left, right):
    expected = pd.date_range(left, right, freq="h", inclusive="left")
    if not frame.index.equals(expected):
        missing = expected.difference(frame.index)
        example = ", ".join(map(str, missing[:3]))
        raise ValueError(f"Часовые свечи неполны. Пропущено {len(missing)}. {example}")
    values = frame.loc[:, OHLC].to_numpy(dtype=float)
    opening, high, low, close = values.T
    if (not np.isfinite(values).all() or (values <= 0).any()
            or (low > np.minimum(opening, close)).any()
            or (high < np.maximum(opening, close)).any()):
        raise ValueError("Некорректный OHLC спотовых свечей")
    return frame


def fetch_hours(public, left, right):
    batches = []
    first = left
    while first < right:
        end = min(first + 1000 * HOUR, right)
        response = public.get_kline(
            category="spot", symbol="ETHUSDT", interval="60",
            start=int(first.timestamp() * 1000),
            end=int(end.timestamp() * 1000) - 1, limit=1000,
        )
        rows = response["result"]["list"]
        index = pd.to_datetime([int(row[0]) for row in rows], unit="ms", utc=True)
        batch = pd.DataFrame([row[1:5] for row in rows], index=index, columns=OHLC, dtype=float)
        batches.append(validate_hourly(batch.sort_index(), first, end))
        first = end
    return pd.concat(batches)


class SpotHistory:
    def __init__(self, public, cache):
        self.public = public
        self.cache = cache

    def window(self, day):
        left = day - HISTORY_DAYS * DAY
        if self.cache.exists():
            hourly = read_hours(self.cache).loc[lambda x: (x.index >= left) & (x.index < day)]
        else:
            hourly = pd.DataFrame()
        if hourly.empty or hourly.index[0] != left:
            hourly = fetch_hours(self.public, left, day)
        else:
            end = hourly.index[-1] + HOUR
            validate_hourly(hourly, left, end)
            if end < day:
                hourly = pd.concat([hourly, fetch_hours(self.public, end, day)])
        validate_hourly(hourly, left, day)
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        temp = self.cache.with_suffix(".tmp")
        hourly.to_csv(temp, index_label="Time")
        temp.replace(self.cache)
        return feature_window(hourly, day)


def read_hours(path):
    data = pd.read_csv(path, index_col=0, float_precision="round_trip")
    data.index = pd.to_datetime(data.index, utc=True)
    return data.loc[:, OHLC]


def daily_features(hourly):
    close = hourly.Close.groupby(hourly.index.floor("D")).last()
    log = np.log(close)
    returns = log.diff()
    sigma = returns.rolling(20).std(ddof=0)
    momentum = log.diff(60)
    side = np.sign(momentum)
    flips = side.ne(side.shift()) & side.notna() & side.shift().notna()
    high, low = log.rolling(20).max(), log.rolling(20).min()
    features = pd.DataFrame({
        "AlignedReturn5": side * log.diff(5) / (sigma * np.sqrt(5)),
        "AlignedReturn20": side * log.diff(20) / (sigma * np.sqrt(20)),
        "MomentumStrength60": (momentum / (sigma * np.sqrt(60))).abs(),
        "MomentumChange5": side * momentum.diff(5) / (sigma * np.sqrt(5)),
        "Efficiency20": log.diff(20).abs() / returns.abs().rolling(20).sum(),
        "FlipCount20": flips.astype(float).rolling(20).sum(),
        "VolatilityRatio": returns.rolling(5).std(ddof=0) / sigma,
        "AdverseMove20": np.where(side > 0, log - high, np.where(side < 0, low - log, 0.)) / sigma,
        "Range20": (high - low) / sigma,
        "Sigma20": sigma,
        "Conflict5": (side * log.diff(5) < 0).astype(float),
        "Conflict20": (side * log.diff(20) < 0).astype(float),
        "Side": side,
    })
    features.iloc[:100] = np.nan
    return features


def feature_window(hourly, day):
    left = day - HISTORY_DAYS * DAY
    relevant = hourly.loc[(hourly.index >= left) & (hourly.index < day)]
    validate_hourly(relevant, left, day)
    values = daily_features(relevant).iloc[-WINDOW:].loc[:, FEATURES].to_numpy(dtype=np.float64)
    if values.shape != (WINDOW, len(FEATURES)) or not np.isfinite(values).all() or (values[:, 9] <= 0).any():
        raise ValueError("В дневном окне F13 есть вырожденные признаки")
    return values[np.newaxis, :, :]
