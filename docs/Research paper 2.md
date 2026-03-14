# Mathematics of AI-driven prediction market trading

**A well-calibrated LLM ensemble combined with Kelly criterion sizing, statistical recalibration, and rigorous edge detection forms the mathematical backbone of a profitable prediction market bot.** The core challenge is converting raw LLM probability outputs—systematically biased toward 50% by RLHF training—into calibrated, tradeable signals with enough edge over market prices to overcome fees, slippage, and adverse selection. Recent academic results show this is achievable: the AIA Forecaster system reached superforecaster-level Brier scores (**~0.081**) on ForecastBench in 2025, and ForecastBench projects LLM–superforecaster parity by **late 2026**. The mathematics presented here spans the complete pipeline from raw model output to position sizing, with concrete formulas, derivations, and implementation code throughout.

---

## 1. The Kelly criterion sizes bets optimally for binary prediction markets

The Kelly criterion maximizes the expected logarithmic growth rate of wealth—the mathematically optimal strategy for long-run capital compounding. Its derivation for prediction markets starts from a simple setup: a trader with bankroll $W$ bets fraction $f$ on a YES contract priced at $p_m$, believing the true probability is $p$.

**Derivation.** If the event occurs (probability $p$), wealth becomes $W(1 + f \cdot \frac{1-p_m}{p_m})$. If it doesn't (probability $1-p$), wealth becomes $W(1-f)$. Maximizing expected log-wealth:

$$G(f) = p \cdot \ln\!\left(1 + f \cdot \tfrac{1-p_m}{p_m}\right) + (1-p) \cdot \ln(1-f)$$

Setting $\frac{dG}{df} = 0$ and solving yields the **Kelly fraction for YES bets**:

$$f^*_{\text{YES}} = \frac{p - p_m}{1 - p_m}$$

By symmetry, for NO bets (when $p < p_m$):

$$f^*_{\text{NO}} = \frac{p_m - p}{p_m}$$

The optimal expected growth rate equals the **KL divergence** between your belief and the market price—a deep result connecting information theory to trading:

$$g^* = p \cdot \ln\!\frac{p}{p_m} + (1-p) \cdot \ln\!\frac{1-p}{1-p_m} = D_{KL}(p \| p_m)$$

This means your information advantage, measured in nats, directly determines your optimal compounding rate.

**Concrete example: $500 bankroll, market at 60%, you estimate 75%.** The edge is 15 percentage points. Full Kelly says bet $f^* = 0.15/0.40 = 37.5\%$ of bankroll = **$187.50**. At $0.60/share, this buys 312.5 YES shares. If YES resolves: profit = $125 (+25% of bankroll). If NO: loss = $187.50 (−37.5%). Expected value = **+$46.88** per trade (9.4% of bankroll).

### Fractional Kelly preserves bankroll at modest cost to growth

Full Kelly is dangerously aggressive. A **50% probability of halving your bankroll** exists under full Kelly sizing. Fractional Kelly at fraction $\lambda$ bets $f = \lambda \cdot f^*$, with these properties:

| Metric | Full Kelly | Half-Kelly ($\lambda=0.5$) | Quarter-Kelly ($\lambda=0.25$) |
|---|---|---|---|
| Growth rate (% of optimal) | 100% | **75%** | 43.75% |
| Variance (% of full) | 100% | **25%** | 6.25% |
| P(50% drawdown) | 50% | 25% | 6.25% |
| P(90% drawdown) | 10% | 1% | **0.01%** |

The growth rate at fraction $\lambda$ follows $g(\lambda) \approx g^* \cdot \lambda(2-\lambda)$. Half-Kelly captures 75% of optimal growth with only 25% of the variance—the best risk-reward tradeoff for uncertain probability estimates.

An elegant interpretation from Beygelzimer et al.: **fractional Kelly at $\lambda$ is equivalent to full Kelly with a blended belief** $p_{\text{adj}} = \lambda \cdot p + (1-\lambda) \cdot p_m$. This naturally encodes uncertainty about your edge, adverse selection risk, and risk aversion in a single parameter.

**Multiple simultaneous bets.** The multi-bet Kelly problem is not additive—optimal simultaneous wagers are smaller than individual Kelly fractions. For $N$ assets with expected excess returns $\mu$ and covariance matrix $\Sigma$, the portfolio-level Kelly is $f^* = \Sigma^{-1}\mu$. In practice, using half-Kelly on each independent bet and capping total deployment at ~40% of bankroll works well.

**Example: $1,000 bankroll, three simultaneous bets at half-Kelly:**

| Bet | $p_m$ | $p_{\text{true}}$ | Edge | Half-Kelly % | Amount | EV |
|---|---|---|---|---|---|---|
| A | 0.50 | 0.60 | 0.10 | 10.0% | $100 | +$20.00 |
| B | 0.30 | 0.40 | 0.10 | 7.1% | $71 | +$23.70 |
| C | 0.70 | 0.80 | 0.10 | 16.7% | $167 | +$23.84 |
| **Total** | | | | **33.8%** | **$338** | **+$67.54** |

---

## 2. From Brier scores to expected profit: the edge conversion mathematics

The Brier score $BS = \frac{1}{N}\sum(f_i - o_i)^2$ measures forecasting accuracy, but converting Brier improvement into dollars requires careful mathematics.

**Murphy decomposition** separates the Brier score into three interpretable components:

$$BS = \underbrace{\textstyle\sum \frac{n_k}{N}(\bar{f}_k - \bar{o}_k)^2}_{\text{Reliability (calibration error)}} - \underbrace{\textstyle\sum \frac{n_k}{N}(\bar{o}_k - \bar{o})^2}_{\text{Resolution (discrimination)}} + \underbrace{\bar{o}(1-\bar{o})}_{\text{Uncertainty (inherent)}}$$

**Reliability** measures calibration—how close forecast probabilities are to observed frequencies. This component directly maps to exploitable trading edges: market miscalibration creates systematic price errors. **Resolution** measures how well forecasts discriminate between events that happen and those that don't. Both matter for profitable trading.

**Expected profit per trade under different market structures.** Under a quadratic market scoring rule, expected profit equals exactly the Brier score difference: $E[\pi] = BS_{\text{market}} - BS_{\text{yours}}$. In a standard continuous double auction (like Polymarket), expected profit per YES contract simplifies to $E[\pi] = p_{\text{true}} - p_m$—the raw edge. If your LLM achieves **Brier 0.10 vs market's 0.15**, the Brier skill score is 33.3%, and average absolute edge per trade is roughly **5–10 cents per dollar wagered**, depending on the distribution of edges across questions.

### The EV framework determines minimum tradeable edges

For a YES contract purchased at price $p_m$:

$$EV = p_{\text{true}} \times (1 - p_m) - (1 - p_{\text{true}}) \times p_m = p_{\text{true}} - p_m$$

The minimum edge to trade profitably must cover all friction costs:

$$\text{edge}_{\min} = \text{fees} + \tfrac{\text{spread}}{2} + \text{slippage} + \text{adverse selection discount}$$

For Polymarket (US, 2025–2026 fee structure: **10 basis points** taker fee):

| Liquidity Level | Fees | Spread | Slippage | Adverse Selection | Min Edge |
|---|---|---|---|---|---|
| High (>$1M daily vol) | 0.2% | 0.5% | 0.5% | 1.0% | **~2.2%** |
| Medium ($100K–$1M) | 0.2% | 1.5% | 1.0% | 2.0% | **~4.7%** |
| Low (<$100K) | 0.2% | 3.0% | 3.0% | 3.0% | **~9.2%** |

The ROI threshold varies dramatically with market price. At $p_m = 0.10$ with 2.1% minimum edge, you need **21% ROI** per trade. At $p_m = 0.50$, you need only **4.2% ROI**. This means low-probability bets require particularly strong conviction.

**Adverse selection adjustment** is critical. When you believe $p \neq p_m$, you must ask why the market disagrees. Model the effective edge as $\text{edge}_{\text{adj}} = \lambda \cdot (\hat{p} - p_m)$ where $\lambda \in [0.3, 0.5]$ represents confidence that your model is more informed than the market. This naturally integrates with fractional Kelly: a combined confidence-and-risk fraction of $\lambda_{\text{conf}} \times \lambda_{\text{Kelly}} \times f^*$ produces the final position size.

---

## 3. Calibrating LLM probabilities with Platt scaling and extremization

LLMs systematically hedge their probability estimates toward 50% due to RLHF training. The AIA Forecaster team confirmed this is **the single most impactful bias to correct**, and that Platt scaling is mathematically equivalent to log-odds extremization.

### Platt scaling fits a sigmoid recalibration

Platt scaling transforms raw scores $s$ through a learned sigmoid: $P(y=1|s) = \frac{1}{1 + \exp(As + B)}$. Parameters $A$ and $B$ are fit by maximizing regularized log-likelihood on calibration data. Platt's regularized targets prevent overfitting on small datasets: for positive examples, $t_+ = (N_+ + 1)/(N_+ + 2)$; for negative, $t_- = 1/(N_- + 2)$.

The gradients for optimization are:

$$\frac{\partial \mathcal{L}}{\partial A} = \sum_i s_i(t_i - p_i), \qquad \frac{\partial \mathcal{L}}{\partial B} = \sum_i (t_i - p_i)$$

**Training data requirements:** Platt scaling (2 parameters) works with **100–200 calibration samples** minimum. Temperature scaling (1 parameter) works with as few as 50. Isotonic regression requires **1,000+ samples** due to its nonparametric flexibility.

**Temperature scaling** is a simpler alternative: $P(y=1|s) = \sigma(s/T)$ where $T > 1$ softens overconfident predictions and $T < 1$ sharpens underconfident ones. Guo et al. (2017) showed temperature scaling is "surprisingly effective" on most datasets. For LLM verbalized probabilities, Wang et al. (2024, arXiv:2410.06707) proposed the **invert softmax trick**: first convert verbalized probabilities to logits via $z = \log(p/(1-p))$, then apply temperature scaling in logit space.

| Method | Parameters | Min Samples | Flexibility | Best For |
|---|---|---|---|---|
| Temperature scaling | 1 | ~50 | Scale only | Uniform over/under-confidence |
| Platt scaling | 2 | ~200 | Sigmoid | Slope + intercept miscalibration |
| Isotonic regression | O(N) | ~1,000+ | Any monotone | Large calibration datasets |

### Extremization corrects the central tendency bias

The log-odds linear extremization transform is:

$$p_{\text{ext}} = \frac{p^\alpha}{p^\alpha + (1-p)^\alpha}, \quad \alpha > 1$$

This pushes predictions away from 50% toward the extremes. **Neyman and Roughgarden (2021)** derived the theoretically optimal extremization parameter as $\alpha \approx \sqrt{3} \approx 1.73$ for large ensembles. Empirical work by Satopaa et al. (2014) found optimal values in the range **$\alpha \in [1.16, 3.92]$** for geopolitical forecasting questions. The parameter $\alpha$ can be fit from calibration data by minimizing the Brier score.

The AIA Forecaster paper showed that Platt scaling and log-odds extremization are mathematically equivalent: the generalized form $\text{logit}(p_{\text{cal}}) = \gamma \cdot \text{logit}(p) + \tau$ encompasses both slope correction ($\gamma$ = extremization) and intercept correction ($\tau$ = base rate adjustment).

---

## 4. Ensemble methods combine multiple LLMs for stronger forecasts

### Linear pooling vs logarithmic pooling

**Simple averaging** (linear pooling) sets $p_{\text{ens}} = \frac{1}{K}\sum p_k$. The Krogh-Vedelsby ambiguity decomposition guarantees this always improves on average individual error:

$$\text{Ensemble Error} = \underbrace{\textstyle\sum w_k \cdot \text{Error}_k}_{\text{Average Error}} - \underbrace{\textstyle\sum w_k(p_k - \bar{p})^2}_{\text{Diversity}} \leq \text{Average Error}$$

Schoenegger et al. (2024, *Science Advances*) demonstrated that a simple median of **12 diverse LLMs** achieved forecasting accuracy statistically indistinguishable from human crowd aggregates.

**Logarithmic pooling** aggregates in log-odds space, which is theoretically superior for prediction markets:

$$\text{logit}(p_{\text{ens}}) = \sum_k w_k \cdot \text{logit}(p_k)$$

This properly handles extreme probabilities—if one model assigns 99.9% and another 50%, logarithmic pooling yields ~97%, while linear pooling gives only ~75%. Log pooling also has the unique property of **external Bayesianity**: the order of aggregation and Bayesian updating doesn't matter.

### Bayesian model averaging weights by track record

BMA computes posterior model weights proportional to the exponentiated negative cumulative log-loss:

$$P(M_k | D) \propto P(M_k) \times \exp\!\left(-N \times \text{LogLoss}_k\right)$$

With uniform priors, this automatically rewards models with better calibration. The connection to Kelly betting is direct: **log score measures the information advantage exploitable in trading**. A model with better log score will, as a Kelly bettor, achieve higher wealth growth.

**Multiplicative weights update (MWU)** provides an online learning algorithm for adapting weights:

$$w_k(t+1) = \frac{w_k(t) \cdot \exp(-\eta \cdot \text{loss}_k(t))}{Z(t)}$$

With learning rate $\eta = \sqrt{2\ln K/T}$ for $K$ models over $T$ rounds, MWU guarantees regret $\leq \sqrt{T\ln K/2}$. For 3 LLMs and 100 resolved questions, the per-question regret bound is **~0.074**—convergence to near-optimal weighting within ~100 observations.

### Concrete ensemble example

Three LLMs forecast an event: Claude gives 0.65, GPT-4o gives 0.72, Gemini gives 0.58. Market price is 0.55. Historical Brier scores: Claude 0.18, GPT-4o 0.15, Gemini 0.22.

| Method | Ensemble Probability | Edge vs Market |
|---|---|---|
| Simple average | 0.650 | 10.0% |
| Inverse-Brier weighted | 0.659 | 10.9% |
| Log-odds pool (equal wt) | 0.652 | 10.2% |
| BMA (50 obs, log-loss weighted) | 0.719 | 16.9% |

Using the inverse-Brier weighted estimate (0.659) with half-Kelly: $f = 0.5 \times (0.659 - 0.55)/(1 - 0.55) = 12.1\%$ of bankroll. For a $1,000 bankroll, this means a **$121 bet** on YES.

**Diversity matters enormously.** With pairwise correlation $\rho = 0.5$ among 3 models, ensemble variance is $\frac{\sigma^2}{3}(1 + 2 \times 0.5) = 0.67\sigma^2$—only a 33% reduction vs 67% with uncorrelated models. Strategies to increase diversity include using different prompts (chain-of-thought vs direct), feeding different retrieved context to each model, varying temperature, and mixing model families.

---

## 5. How many trades before you know your edge is real?

### Power analysis determines required sample size

For trades at market price $p_m$ with suspected edge $\delta$, the number of trades needed for statistical significance at $\alpha = 0.05$ with 80% power:

$$N = \frac{(z_\alpha + z_\beta)^2 \cdot p_m(1-p_m)}{\delta^2} = \frac{6.18 \times p_m(1-p_m)}{\delta^2}$$

| Edge ($\delta$) | At $p_m = 0.50$ | At $p_m = 0.80$ | At $p_m = 0.95$ |
|---|---|---|---|
| 3% | **1,717 trades** | 1,099 | 327 |
| 5% | **618 trades** | 396 | 118 |
| 10% | **155 trades** | 99 | 30 |

A 5% edge at even odds requires **~620 trades** to confirm with statistical significance. This means months of trading before you can distinguish skill from luck.

**Wald's Sequential Probability Ratio Test (SPRT)** reduces this by ~50% through adaptive early stopping. After each trade, compute the log-likelihood ratio $\Lambda_n = \sum \log\frac{L(R_i|H_1)}{L(R_i|H_0)}$. Stop and accept $H_1$ (edge exists) when $\Lambda_n \geq 2.77$; accept $H_0$ (no edge) when $\Lambda_n \leq -1.56$ (for $\alpha = 0.05$, $\beta = 0.20$).

### Sharpe ratio quantifies risk-adjusted returns

Per-trade Sharpe for prediction markets: $SR = \delta / \sqrt{p_m(1-p_m)}$. At even odds with 5% edge, $SR_{\text{trade}} = 0.10$. **Annualized Sharpe** scales by $\sqrt{N_{\text{trades/year}}}$: with 500 trades/year and 5% edge, $SR_{\text{annual}} = 0.10 \times \sqrt{500} = 2.24$—an excellent risk-adjusted return that would rank among top quantitative hedge funds.

For context: the S&P 500's long-term Sharpe is ~0.4, Berkshire Hathaway's 1976–2017 Sharpe was 0.79, and top quant funds achieve 1.0–2.0.

**Worked example: 200 trades, 58% win rate at 50% odds.** Under $H_0$ (no edge), expected win rate is 50%. Standard error = $\sqrt{0.25/200} = 0.0354$. Z-statistic = $(0.58 - 0.50)/0.0354 = 2.26$, yielding p-value = **0.012**—significant at $\alpha = 0.05$. The edge is 8% with 95% CI of [1.1%, 14.9%]. Annualized Sharpe = $0.16 \times \sqrt{200} = 2.26$.

### Adverse selection detection in order books

The Glosten-Milgrom model shows that a fraction $\mu$ of informed traders creates a bid-ask spread even with zero-profit market making. The spread decomposes into order processing costs, inventory risk, and adverse selection components. Key detection metrics:

- **Post-trade price movement**: If prices systematically move against you after your limit orders fill, you're being adversely selected. Measure average mark-to-market P&L $\tau$ seconds post-fill.
- **Realized spread vs quoted spread**: Realized spread = $2 \times (\text{trade price} - \text{midpoint after } \tau)$. If realized < quoted, the difference quantifies adverse selection cost.
- **VPIN** (Volume-Synchronized Probability of Informed Trading): Measures order flow imbalance normalized by volume to detect toxic flow in real time.

---

## 6. The complete calibration pipeline in Python

The following implementation chains together every mathematical component—from raw LLM output to position size:

```python
import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.special import expit  # sigmoid
from sklearn.isotonic import IsotonicRegression
from dataclasses import dataclass, field

# ─── PLATT SCALING ───────────────────────────────────────────
class PlattScaler:
    """P(y=1|s) = 1/(1 + exp(A*s + B)), fit via regularized MLE."""
    def __init__(self):
        self.A, self.B = 0.0, 0.0

    def fit(self, scores, labels):
        s, y = np.asarray(scores, float), np.asarray(labels, float)
        N_pos, N_neg = y.sum(), (1 - y).sum()
        t = np.where(y == 1, (N_pos + 1)/(N_pos + 2), 1/(N_neg + 2))

        def nll(params):
            A, B = params
            p = np.clip(expit(-(A * s + B)), 1e-10, 1 - 1e-10)
            return -np.sum(t * np.log(p) + (1 - t) * np.log(1 - p))

        res = minimize(nll, [0.0, 0.0], method='L-BFGS-B')
        self.A, self.B = res.x
        return self

    def predict(self, scores):
        return expit(-(self.A * np.asarray(scores, float) + self.B))

# ─── TEMPERATURE SCALING ─────────────────────────────────────
class TemperatureScaler:
    """P(y=1|s) = sigmoid(logit(s)/T), single parameter T."""
    def __init__(self):
        self.T = 1.0

    def fit(self, probs, labels):
        p = np.clip(np.asarray(probs, float), 1e-10, 1 - 1e-10)
        logits = np.log(p / (1 - p))
        y = np.asarray(labels, float)

        def nll(T):
            q = np.clip(expit(logits / max(T, 0.01)), 1e-10, 1 - 1e-10)
            return -np.mean(y * np.log(q) + (1 - y) * np.log(1 - q))

        self.T = minimize_scalar(nll, bounds=(0.01, 20), method='bounded').x
        return self

    def predict(self, probs):
        p = np.clip(np.asarray(probs, float), 1e-10, 1 - 1e-10)
        return expit(np.log(p / (1 - p)) / self.T)

# ─── EXTREMIZATION ───────────────────────────────────────────
def extremize(p, alpha=1.73):
    """Log-odds extremization: p^α / (p^α + (1-p)^α)."""
    p = np.clip(np.asarray(p, float), 1e-10, 1 - 1e-10)
    pa, qa = p**alpha, (1 - p)**alpha
    return pa / (pa + qa)

def fit_alpha(probs, outcomes, bounds=(0.5, 5.0)):
    """Fit extremization α by minimizing Brier score."""
    p, o = np.asarray(probs, float), np.asarray(outcomes, float)
    def bs(a): return np.mean((extremize(p, a) - o)**2)
    return minimize_scalar(bs, bounds=bounds, method='bounded').x

# ─── ENSEMBLE COMBINATION ────────────────────────────────────
def log_pool(probs, weights=None):
    """Logarithmic opinion pool: weighted average of log-odds."""
    p = np.clip(np.asarray(probs, float), 1e-10, 1 - 1e-10)
    logits = np.log(p / (1 - p))
    w = np.ones(len(p)) / len(p) if weights is None else np.asarray(weights)
    w = w / w.sum()
    return float(expit(w @ logits))

def update_weights_mwu(weights, losses, eta=0.1):
    """Multiplicative weights update after observing losses."""
    w = np.asarray(weights) * np.exp(-eta * np.asarray(losses))
    return w / w.sum()

# ─── EVALUATION METRICS ──────────────────────────────────────
def brier_score(probs, outcomes):
    return np.mean((np.asarray(probs) - np.asarray(outcomes))**2)

def expected_calibration_error(probs, outcomes, n_bins=15):
    p, o = np.asarray(probs), np.asarray(outcomes)
    edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        mask = (p > edges[i]) & (p <= edges[i + 1])
        if mask.sum() == 0: continue
        ece += mask.sum() / len(p) * abs(o[mask].mean() - p[mask].mean())
    return ece

# ─── KELLY CRITERION ─────────────────────────────────────────
def kelly_fraction(p_true, p_market, side='auto'):
    """Kelly fraction for binary prediction market."""
    if side == 'auto':
        side = 'YES' if p_true > p_market else 'NO'
    if side == 'YES':
        return max(0, (p_true - p_market) / (1 - p_market))
    else:
        return max(0, (p_market - p_true) / p_market)

# ─── FULL PIPELINE ───────────────────────────────────────────
@dataclass
class PredictionMarketPipeline:
    """Raw LLM probabilities → calibrated, sized trading signal."""
    calibrator: PlattScaler = field(default_factory=PlattScaler)
    alpha: float = 1.73           # extremization parameter
    kelly_frac: float = 0.25     # quarter-Kelly default
    max_position: float = 0.05   # 5% max per position
    min_edge: float = 0.03       # 3% minimum edge to trade
    model_weights: np.ndarray = None

    def fit(self, cal_probs, cal_outcomes):
        """Fit calibrator + extremization on historical data."""
        self.calibrator.fit(cal_probs, cal_outcomes)
        cal = self.calibrator.predict(cal_probs)
        self.alpha = fit_alpha(cal, cal_outcomes)

    def signal(self, raw_probs, p_market):
        """
        Process one prediction opportunity.
        raw_probs: list of LLM probability estimates
        p_market: current market price
        Returns dict with direction, size, and diagnostics.
        """
        # Step 1-2: Calibrate each LLM output
        calibrated = [float(self.calibrator.predict(np.array([p]))[0])
                      for p in raw_probs]

        # Step 3: Ensemble via log-odds pooling
        p_ens = log_pool(calibrated, self.model_weights)

        # Step 4: Extremize
        p_final = float(extremize(p_ens, self.alpha))

        # Step 5: Kelly sizing
        edge = abs(p_final - p_market)
        if edge < self.min_edge:
            return {'direction': 'NO_TRADE', 'size': 0, 'edge': edge,
                    'p_final': p_final}

        side = 'YES' if p_final > p_market else 'NO'
        f = kelly_fraction(p_final, p_market, side)
        size = min(self.kelly_frac * f, self.max_position)

        return {
            'direction': side,
            'size': round(size, 4),
            'edge': round(edge, 4),
            'p_final': round(p_final, 4),
            'p_ensemble': round(p_ens, 4),
            'calibrated': [round(c, 4) for c in calibrated],
            'kelly_full': round(f, 4),
            'ev_per_dollar': round(edge, 4),
        }
```

**Pipeline walkthrough with numbers.** Three LLMs estimate 62%, 58%, 65% for an event priced at 50%. After Platt scaling (fitted $A = -1.2$, $B = 0.3$), calibrated values become ~64%, 60%, 67%. Log-odds pooling yields 63.7%. Extremization with $\alpha = 1.73$ pushes this to ~68.2%. Edge = 18.2%. Full Kelly = $0.182/0.50 = 36.4\%$. At quarter-Kelly with 5% cap: position = **min(9.1%, 5%) = 5%** of bankroll. For a $1,000 bankroll, bet **$50** on YES.

---

## 7. What the latest research says about LLM forecasting performance

### ForecastBench tracks the closing gap between LLMs and superforecasters

ForecastBench, created by the Forecasting Research Institute (Karger et al., ICLR 2025), is the definitive dynamic benchmark. Its ~1,000 binary questions per round span geopolitics, economics, finance, and science, sourced from Metaculus, Polymarket, ACLED, and FRED. All questions concern future events with no known answer at submission, eliminating data contamination.

The latest ForecastBench results show superforecasters at **Brier 0.081** (difficulty-adjusted), with the best LLMs (GPT-4.5) at **0.101** and the leading AI system ensembles at **0.103**. The improvement rate is **~0.016 Brier points per year** for state-of-the-art LLMs, with projected human–AI parity by **November 2026** (95% CI: December 2025 – January 2028). A critical finding: LLMs exhibit a **0.994 correlation** with market prices when shown them, meaning they largely copy rather than improve on market forecasts.

### Three architectural breakthroughs drive most of the improvement

The literature converges on three techniques that account for nearly all gains:

**Retrieval-augmented generation is the single most important component.** Halawi et al. (NeurIPS 2024) showed that without retrieval, even GPT-4 performs near-random on forecasting (Brier ~0.25). With optimized retrieval + reasoning, their system achieved **Brier 0.179** vs human crowd's 0.149 on 914 questions. The AIA Forecaster team (Bridgewater, arXiv:2511.07678) corroborated this: "Effective search is arguably the most critical component of effective forecasting." Their multi-agent agentic search—where each agent has full discretion over queries, conditioned on previous results—achieved **superforecaster parity** on ForecastBench.

**Extremization/Platt scaling counters RLHF hedging.** AIA Forecaster's key technical insight is that RLHF-trained LLMs systematically attenuate forecasts toward 50%, and simple post-hoc extremization yields large Brier improvements. They proved Platt scaling is mathematically equivalent to generalized log-odds extremization.

**RL fine-tuning on outcome data closes ~65% of the gap to market prices.** Turtel et al. (2025) fine-tuned R1-14B via self-play on 9,800 Polymarket questions, improving Brier from 0.214 to ~0.197 (matching o1 performance). Lightning Rod Labs' Foresight-32B used RL fine-tuning on Qwen3-32B to achieve **Brier 0.199** on 251 live Polymarket questions, outperforming all tested frontier LLMs despite being 10–100x smaller. Critically, its ECE dropped from **19.2% to 6.0%**—a 69% reduction in calibration error.

### Best published Brier scores across systems

| System | Brier Score | Benchmark | Year |
|---|---|---|---|
| Expert forecasters (Metaculus) | **0.023** | 464 questions | 2025 |
| Superforecasters (ForecastBench) | **0.081** | Difficulty-adjusted | 2025 |
| AIA Forecaster | ~0.081 | ForecastBench | 2025 |
| GPT-4.5 | 0.101 | ForecastBench | 2025 |
| o3 (with AskNews retrieval) | 0.135 | 464 Metaculus Qs | 2025 |
| Polymarket market consensus | 0.170 | 251 live questions | 2025 |
| Foresight-32B (RL fine-tuned) | 0.199 | 251 Polymarket Qs | 2025 |
| Zero-shot GPT-4 (no retrieval) | ~0.250 | Various | 2024 |

---

## 8. Connecting the mathematics: where profitable edges actually come from

The mathematical framework above reveals several key structural insights about where genuine trading edges can exist.

**The log score–Kelly connection is fundamental.** Maximizing log scoring rule performance is mathematically identical to maximizing Kelly growth rate. This means evaluating your forecasting system by log loss directly predicts its profitability as a Kelly bettor. A forecaster with cumulative log loss $L_{\text{you}}$ vs the market's $L_{\text{market}}$ will compound wealth at rate $\exp(L_{\text{market}} - L_{\text{you}})$ relative to the market.

**Edge size interacts non-linearly with growth.** The Kelly growth rate equals $D_{KL}(p \| p_m)$, which for small edges $\delta$ near 50% odds approximates $2\delta^2$. This means doubling your edge quadruples your growth rate—small improvements in calibration have outsized impact on compounding. At a 5% edge on even-odds bets, the per-trade growth rate is 0.5%, compounding to **~170% annually** over 500 trades at full Kelly (or ~57% at half-Kelly).

**The real bottleneck is calibration, not raw accuracy.** A model can achieve high accuracy but poor profitability if miscalibrated (saying 70% when true frequency is 85%). Conversely, a well-calibrated model that honestly reports 55% when the market says 50% generates consistent profits. The pipeline of Platt scaling → extremization → ensemble exists precisely to convert raw accuracy into calibrated, tradeable probabilities. ECE of 5% translates to roughly **5 cents of average edge per dollar wagered**—enough to be profitable on liquid markets after costs.

**Statistical validation requires patience.** With a 5% edge, you need **~620 trades** at even odds to confirm significance. At 2 trades per day, this takes a full year. Sequential testing (SPRT) can halve this, but the fundamental tension remains: small edges require large sample sizes. The practical implication is that any serious prediction market bot needs to trade across many markets simultaneously to accumulate statistical evidence quickly, while managing correlation between positions.

## Conclusion

The mathematics of profitable prediction market trading rests on a chain of precisely connected transformations: raw LLM probabilities are recalibrated through Platt scaling or temperature scaling, extremized to correct for RLHF hedging bias, combined across models via logarithmic opinion pooling, converted to edge estimates against market prices, filtered through minimum EV thresholds accounting for fees and adverse selection, and finally sized through fractional Kelly to maximize long-run growth while controlling drawdown risk.

Three insights emerge as non-obvious from this analysis. First, the growth rate–KL divergence equivalence means that **information-theoretic measures of forecast quality directly predict trading profitability**, unifying the forecasting and trading problems. Second, the quadratic relationship between edge size and Kelly growth rate ($g \approx 2\delta^2$ near even odds) means **marginal improvements in calibration have superlinear payoffs**—reducing ECE from 8% to 5% more than doubles compounding speed. Third, the academic literature strongly suggests that **retrieval quality, not model size, is the binding constraint** on LLM forecasting—a well-scaffolded small model with excellent search consistently outperforms frontier models without retrieval.

The projected convergence of LLM forecasting to superforecaster levels by late 2026 suggests a narrowing window for easy prediction market alpha. The enduring edge will likely belong to systems that combine the best retrieval infrastructure, the most rigorous calibration pipelines, and the most disciplined position management—exactly the mathematical framework presented here.