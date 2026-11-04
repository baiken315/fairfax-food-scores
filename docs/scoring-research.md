# Scoring research notes (PLAN Phase 5.2)

Compiled 2026-10-22. Purpose: justify the candidate weight sets in `scoring.py` and decide what
`ffx calibrate` should test. These notes inform hypotheses. They do not settle the weights; the
calibration results on Fairfax data do that.

## Findings

### 1. Overall scores are weakly tied to outbreaks; specific items matter more
- **Jones et al. 2004** (Tennessee, 167,574 inspections, 1993–2000): restaurants linked to outbreaks
  did **not** have different mean inspection scores from other restaurants. None of the 12 most-cited
  violations were critical food-safety hazards.
  [EID 10(4):688–692](https://wwwnc.cdc.gov/eid/article/10/4/03-0343_article)
- **Petran, White & Hedberg 2012** (Minnesota; J. Food Prot. 75(11):2007–2015, and Petran's
  [2012 dissertation](https://conservancy.umn.edu/items/fb86e218-b048-4e29-8c37-02e5e3af78b7)):
  outbreak restaurants had significantly more violations on routine inspections. A meta-analysis across
  norovirus, Salmonella and C. perfringens outbreaks found **11 violations** significantly more likely
  at outbreak restaurants.
- **Implication:** a plain violation count says little. Which items are OUT should drive the score.
  Present the score as "how this place does on inspections", never as "how likely you are to get sick".

### 2. Good-retail items are not obviously low-value
Petran's 11 items, mapped from FDA Form 3-A to Fairfax's 56-item form (dissertation ch. 3, Table 6):

| Fairfax item | Item | Section | Relative risk (where legible) |
|---|---|---|---|
| 6 | Proper eating, tasting, drinking, tobacco | Risk factor | 4.59 |
| 9 | No bare-hand contact with RTE food | Risk factor | 2.8 |
| 20 | Proper cooling time and temperature | Risk factor | ~11 (wide CI) |
| 22 | Proper cold holding | Risk factor | 1.83 |
| 33 | Proper cooling methods | Good retail | 5.16 |
| 37 | Food properly labeled | Good retail | 3.53 |
| 39 | Contamination prevented during prep/storage/display | Good retail | 2.01 |
| 44 | Utensils, equipment, linens properly stored | Good retail | 2.41 |
| 45 | Single-use articles properly stored and used | Good retail | ~10.7 (wide CI) |
| 49 | Non-food-contact surfaces clean | Good retail | 3.38 |
| 56 | Ventilation and lighting | Good retail | 1.72 |

The relative-risk figures come from a PDF table that extracted poorly. Treat them as approximate and
check against the published article before quoting them.

- **7 of 11 are good retail practices.** A plausible reading is that upkeep problems are a marker of weak
  management, which is what actually causes outbreaks.
- **Implication:** weighting risk factors 4× good retail (`rf-heavy`, the current placeholder default)
  is an untested assumption. Two weight sets now test it: `flat` (equal weights) and `petran`
  (those 11 items ×2).

### 3. History and repeats predict future problems
- Chicago's food-inspection forecasting model found prior serious/critical violations among the strongest
  predictors of a new critical violation.
  [Harvard Data-Smart summary](https://datasmart.hks.harvard.edu/news/article/delivering-faster-results-with-food-inspection-forecasting-631)
- **Implication:** supports a REPEAT multiplier and a location score built from recency-weighted history.
  The `next_*` calibration targets measure exactly this.

### 4. Inspector variability is a major confounder
- **Kannan, Shapiro & Bilgic 2019**, an independent analysis of the Chicago model: the most influential
  feature was *which group of inspectors* did the last inspection. They concluded the model's gains
  "might not be due to the model", and recommended investigating "differences in violation citations
  across sanitarians". [arXiv:1910.04906](https://arxiv.org/abs/1910.04906)
- **Implication for Fairfax:** two locations can differ only because a stricter inspector visited one of them.
  The parser deliberately does not store inspector names (privacy). Options:
  1. Do nothing, and accept the noise.
  2. Store an opaque, salted hash of the inspector name, used only for calibration and adjustment and never published
     or exported. This would allow testing for, and adjusting away, inspector-to-inspector differences.
  3. Use a coarser proxy, such as inspection area or supervisory district.
  **Decided 2026-10-23:** inspector data is not collected or used. An inspector adjustment was briefly tested and did
  not improve prediction of the next inspection, and rating individual inspectors by name is out of scope for this site.

### 5. Corrected during inspection (CDI)
- No published evidence was found either for or against discounting violations that were corrected on site.
- NYC, LA County and Boston all score from violations the inspector observes. None of the sources found
  describe a discount for on-site correction, but none explicitly rule one out either.
  [NYC grading](https://www.nyc.gov/site/doh/business/food-operators/letter-grading-for-restaurants.page) ·
  [LA County grading](https://publichealth.lacounty.gov/eh/inspection/grading-posting-requirements-retail-food-facilities.htm) ·
  [Boston grade sheet](https://content.boston.gov/sites/default/files/file/document_files/2016/12/restaurant_grade_info_sheet_final.pdf)
- Argument against discounting: the hazard existed until the inspector arrived, so CDI says more about
  the visit than about how the place normally runs.
- **Implication:** default to no discount (`cdi_mult = 1.0`). Keep `rf-heavy-cdi` (×0.75) and `v0-tier` (×0.6)
  as comparisons; calibration will show whether the discount helps.

### 6. How other systems turn points into grades
- **NYC:** points, lower is better. Public-health hazards ≥7 points, critical ≥5, general ≥2, scaled by a
  1–5 "condition level" (how extensive the problem is). Bands: A 0–13, B 14–27, C 28+.
- **LA County:** starts at 100 and deducts by risk; letter bands at 90/80/70, with a number score shown below 70.
  Since 2017, two major violations cost extra points.
- **San Diego County:** majors 4 points, minors 2, good retail 1. A reported 99% of restaurants got an A,
  which is the failure mode to avoid ([NBC San Diego](https://www.nbcsandiego.com/news/local/99-of-san-diego-restaurants-earn-a-grades-bringing-usefulness-of-system-into-question/25381/)).
- **Implication:** the provisional 90/80/70/60 cut-offs follow LA's shape. Choose the final cut-offs so the
  grade distribution actually separates locations; check the share of A grades after the backfill.

### 7. FDA's own measure is a compliance rate
- FDA's Retail Food Risk Factor Studies report the **percentage of applicable items IN compliance**, by item
  and by risk factor, not point totals.
  [2017–2018 restaurant study](https://www.fda.gov/media/169389/download)
- **Implication:** the `rate` weight set mirrors FDA's measure. It needs no point values, which makes it
  the easiest to explain publicly.

## What to run once the backfill finishes
1. `ffx calibrate --out data/calibration-<date>.md`, which compares all 7 weight sets at shrinkage strengths 0/1/2/4.
2. Check the letter distribution (`SELECT letter, COUNT(*) FROM scores WHERE weight_set=? GROUP BY 1`)
   and the share of locations with insufficient data. Tune `MIN_EFFECTIVE_N` and the cut-offs.
3. Pick the default weight set based on the next-inspection targets first, complaints second, and follow-up last
   (follow-up is a same-visit check, not a prediction).

## Calibration results, 2026-10-23 (full backfill: 13,094 scoreable inspections, 3,821 locations)
Full table: `data/calibration-2026-10-23.md`.

- **Predicting the next inspection:** every weight set scores AUC ≈ 0.75 for "next inspection has a Priority
  risk-factor OUT" (49% of next inspections do). All the intervals overlap, so **the choice of weights doesn't
  change how well locations are ranked.** Pick the set for explainability and grade spread.
- **Shrinkage:** prior strength **1** is best for every weight set (0.754–0.758, against 0.745–0.748 at 2 and 0.73 at 4).
- **Follow-up (same visit):** `v0-tier` agrees best (0.869), meaning inspectors' follow-up decisions track the count of
  citations with a CDI discount. This shows how inspectors decide, not which places are riskier.
- **Complaints:** AUC ≈ 0.545 for every set. Complaints are only weakly related to inspection results, which matches
  the literature. Keep them out of the score.
- **Grade spread at the provisional 90/80/70/60 cut-offs:** `v0-tier` gives 93% A (the San Diego problem); `rate` 78% A;
  `flat` 69% A; `rf-heavy` 47% A / 38% B / 10% C / 2% D / 0.4% F. 2.5% of locations have no letter (insufficient data).
- **Unmatched complaints (478 of 1,897):** mostly unlicensed or mobile vendors, hotels, gyms and blank names. A few look like
  real restaurants that don't appear in the permit layer. A geocoder fallback would add little.
