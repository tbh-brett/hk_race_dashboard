# Trial → Next Race study  (4,741 pairs, gap ≤ 60d)

Generated: 2026-04-28 12:20

## Headline rates

- Overall win rate:     **0.087**
- Overall top-3 rate:   **0.236**
- Overall avg finish%:  **0.550**  (lower = better)

## Confusion matrix — trial sentiment × race outcome

```
outcome            win   top3    mid   back     n
trial_sentiment                                  
++               0.139  0.204  0.229  0.428  1277
+                0.091  0.156  0.241  0.513  1830
0                0.061  0.106  0.221  0.611   620
-                0.032  0.092  0.161  0.716  1014
```

## Hit rates by trial sentiment
```
### Trial sentiment
                    n    win   top3  top_half  avg_finish_pct
trial_sentiment                                              
+                1830  0.091  0.246     0.514           0.526
++               1277  0.139  0.343     0.592           0.469
-                1014  0.032  0.123     0.309           0.667
0                 620  0.061  0.168     0.416           0.597
```

## Hit rates by trial sentiment AND distance match (≤200m)
```
                               n    win   top3
trial_sentiment dist_match                    
+               False        540  0.100  0.250
                True        1290  0.087  0.245
++              False        290  0.141  0.379
                True         987  0.139  0.332
-               False        304  0.053  0.164
                True         710  0.023  0.106
0               False        189  0.079  0.259
                True         431  0.053  0.128
```

## Concealed-form trials (hidden positive)
```
              n    win   top3
concealed                    
False      4298  0.087  0.231
True        443  0.088  0.280
```

## Trial winners (won the trial outright)
```
              n    win   top3
won_trial                    
False      4093  0.074  0.212
True        648  0.173  0.389
```

## Improvers (better than previous trial)
```
             n    win   top3
improver                    
False     2955  0.096  0.248
True      1786  0.073  0.215
```

## Stacked: improver × positive sentiment
```
                   n    win   top3
improver pos                      
False    False  1149  0.047  0.152
         True   1806  0.127  0.310
True     False   485  0.033  0.111
         True   1301  0.088  0.254
```

## Archetype counts
```
  HONEST_GOOD        25
  FALSE_POSITIVE     25
  HIDDEN_GEM         25
  HONEST_POOR        25
```

### Recent HONEST_GOOD (latest 25)
- **GRAND NOVA**  trial 2026-03-16 (++, 1000m) → race 2026-04-22 placed 2/12 (1000m)
- **TURF PHOENIX**  trial 2026-04-09 (+, 1200m) → race 2026-04-22 placed 3/12 (1200m)
- **GRAND NOVA**  trial 2026-03-27 (+, 1000m) → race 2026-04-22 placed 2/12 (1000m)
- **ACE WAR**  trial 2026-04-09 (+, 1200m) → race 2026-04-22 placed 2/12 (1800m)
- **NEBRASKAN**  trial 2026-04-09 (+, 1200m) → race 2026-04-22 placed 1/12 (1200m)
- **NEBRASKAN**  trial 2026-03-27 (+, 1000m) → race 2026-04-22 placed 1/12 (1200m)
- **THUNDER PRINCE**  trial 2026-04-09 (++, 1200m) → race 2026-04-22 placed 1/12 (1200m)
- **CASA OF HONOR**  trial 2026-04-09 (++, 1050m) → race 2026-04-22 placed 1/12 (1000m)
- **TURF PHOENIX**  trial 2026-03-14 (+, 1200m) → race 2026-04-22 placed 3/12 (1200m)
- **MASTER PAYMENT**  trial 2026-03-16 (+, 1000m) → race 2026-04-19 placed 2/14 (1000m)
- **TARGET AUDIENCE**  trial 2026-03-30 (+, 800m) → race 2026-04-19 placed 1/12 (1200m)
- **SKY JEWELLERY**  trial 2026-03-16 (+, 1200m) → race 2026-04-19 placed 1/12 (1400m)
- **NAUTICAL FORCE**  trial 2026-03-16 (+, 1200m) → race 2026-04-19 placed 1/14 (1800m)
- **CHILL EASY**  trial 2026-03-19 (+, 1200m) → race 2026-04-19 placed 3/12 (1400m)
- **MASTER PAYMENT**  trial 2026-03-02 (++, 800m) → race 2026-04-19 placed 2/14 (1000m)

### Recent FALSE_POSITIVE (latest 25)
- **SOLAR RIVER**  trial 2026-02-24 (+, 800m) → race 2026-04-22 placed 10/13 (1200m)
- **HEALTHY HEALTHY**  trial 2026-03-16 (+, 1000m) → race 2026-04-22 placed 8/12 (1000m)
- **HYANNIS STAR**  trial 2026-03-30 (+, 800m) → race 2026-04-22 placed 9/12 (1000m)
- **HEALTHY HEALTHY**  trial 2026-03-27 (+, 1000m) → race 2026-04-22 placed 8/12 (1000m)
- **ATOMIC BEAUTY**  trial 2026-04-02 (+, 1200m) → race 2026-04-22 placed 11/12 (1650m)
- **CENTRAL BANK**  trial 2026-03-31 (++, 1050m) → race 2026-04-22 placed 10/12 (1000m)
- **MASTEROFMYUNIVERSE**  trial 2026-04-09 (++, 1200m) → race 2026-04-22 placed 9/12 (1200m)
- **COPPER CORE**  trial 2026-04-09 (+, 1050m) → race 2026-04-22 placed 11/12 (1000m)
- **TOPSPIN KING**  trial 2026-04-09 (-, 1050m) → race 2026-04-22 placed 10/12 (1000m)
- **YEUX DE LIFELINE**  trial 2026-02-27 (+, 1200m) → race 2026-04-22 placed 8/13 (1200m)
- **COLOURFUL WINNER**  trial 2026-03-19 (+, 1200m) → race 2026-04-19 placed 9/12 (1200m)
- **DASHING PEACH**  trial 2026-03-26 (+, 1050m) → race 2026-04-19 placed 13/14 (1400m)
- **SONIC BOOM**  trial 2026-03-27 (++, 1200m) → race 2026-04-19 placed 11/12 (1200m)
- **COLOURFUL WINNER**  trial 2026-03-02 (++, 800m) → race 2026-04-19 placed 9/12 (1200m)
- **ROBOT LUCKY STAR**  trial 2026-03-14 (++, 1200m) → race 2026-04-19 placed 8/12 (1200m)

### Recent HIDDEN_GEM (latest 25)
- **GRAND NOVA**  trial 2026-03-02 (0, 800m) → race 2026-04-22 placed 2/12 (1000m)
- **MEGA MASTERMIND**  trial 2026-04-10 (0, 1200m) → race 2026-04-19 placed 2/14 (1600m)
- **NAUTICAL FORCE**  trial 2026-04-10 (-, 1600m) → race 2026-04-19 placed 1/14 (1800m)
- **MUST GO**  trial 2026-04-09 (-, 1200m) → race 2026-04-19 placed 1/11 (1200m)
- **NAUTICAL FORCE**  trial 2026-03-06 (-, 1200m) → race 2026-04-19 placed 1/14 (1800m)
- **KA YING GENERATION**  trial 2026-03-27 (0, 1600m) → race 2026-04-19 placed 2/14 (1800m)
- **ZETTA FORCE**  trial 2026-03-16 (-, 1200m) → race 2026-04-15 placed 3/13 (1650m)
- **ZETTA FORCE**  trial 2026-02-27 (-, 1000m) → race 2026-04-15 placed 3/13 (1650m)
- **KINGLY DEMEANOR**  trial 2026-04-02 (0, 1200m) → race 2026-04-15 placed 2/12 (1800m)
- **ZETTA FORCE**  trial 2026-03-27 (0, 1200m) → race 2026-04-15 placed 3/13 (1650m)
- **TALENTS CHAMPION**  trial 2026-02-24 (-, 800m) → race 2026-04-12 placed 3/6 (1000m)
- **FLASHING FIGHTER**  trial 2026-03-10 (0, 1200m) → race 2026-04-12 placed 1/12 (1200m)
- **SECRET INGREDIENT**  trial 2026-03-16 (-, 1000m) → race 2026-04-12 placed 2/6 (1000m)
- **SECRET INGREDIENT**  trial 2026-02-24 (-, 800m) → race 2026-04-12 placed 2/6 (1000m)
- **FORTUNE LINK**  trial 2026-02-27 (0, 1000m) → race 2026-04-12 placed 1/12 (1400m)

### Recent HONEST_POOR (latest 25)
- **YEUX DE LIFELINE**  trial 2026-04-10 (-, 1200m) → race 2026-04-22 placed 8/13 (1200m)
- **YEUX DE LIFELINE**  trial 2026-03-27 (-, 1000m) → race 2026-04-22 placed 8/13 (1200m)
- **WITHOUT RHYME**  trial 2026-04-10 (-, 1200m) → race 2026-04-22 placed 12/12 (1200m)
- **SOLAR RIVER**  trial 2026-04-09 (-, 1200m) → race 2026-04-22 placed 10/13 (1200m)
- **CENTRAL BANK**  trial 2026-03-16 (-, 1000m) → race 2026-04-22 placed 10/12 (1000m)
- **ZHOU GONGJIN**  trial 2026-04-02 (-, 1200m) → race 2026-04-22 placed 11/13 (1200m)
- **SOLAR RIVER**  trial 2026-03-14 (-, 1000m) → race 2026-04-22 placed 10/13 (1200m)
- **WITHOUT RHYME**  trial 2026-03-14 (-, 1200m) → race 2026-04-22 placed 12/12 (1200m)
- **WITHOUT RHYME**  trial 2026-02-27 (-, 1200m) → race 2026-04-22 placed 12/12 (1200m)
- **WITHOUT RHYME**  trial 2026-03-27 (-, 1200m) → race 2026-04-22 placed 12/12 (1200m)
- **EMERGING STAR**  trial 2026-03-16 (-, 1000m) → race 2026-04-19 placed 14/14 (1000m)
- **MAGNEMITE**  trial 2026-03-24 (-, 1000m) → race 2026-04-19 placed 10/12 (1200m)
- **MAGNEMITE**  trial 2026-03-10 (-, 1200m) → race 2026-04-19 placed 10/12 (1200m)
- **EMERGING STAR**  trial 2026-02-27 (-, 1000m) → race 2026-04-19 placed 14/14 (1000m)
- **EMERGING STAR**  trial 2026-03-27 (-, 1000m) → race 2026-04-19 placed 14/14 (1000m)