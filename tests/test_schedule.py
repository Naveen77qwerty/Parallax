"""
endgame/schedule.py tested against frozen times (freezegun) at the exact
boundaries that matter: 23:59 Sep 2 (last carry entry), 14:29/14:31 ET Sep 3
(convexity entry gate), 08:29/08:31 ET Sep 4 (NFP), 10:44/10:46 ET Sep 4
(flatten deadline), 10:59/11:01 ET Sep 4 (submission deadline). Off-by-one
here is the single bug most likely to leave a position open at judging time.
"""
