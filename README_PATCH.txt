JP Planner v6.8 - staffing rule patch

Changes:
- Default staffing is now 1 staff member per started block of 30 participants.
  1-30 = 1
  31-60 = 2
  61-90 = 3
  91-120 = 4
  etc.
- The staff field remains manually editable on the assignments screen.
- A manually saved staff count remains authoritative for that reservation/reference.

Upload/overwrite only:
- app.py
- templates/index.html
