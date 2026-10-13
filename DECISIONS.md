# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.



## res in my own word

## 1. Indexes

I created indexes to make searches faster and avoid duplicate data. The employee code index makes sure each employee has a unique code. The employee department and joining date indexes help with employee lists and department reports. The attendance employee code and date index prevents an employee from having two attendance records for the same day. Other indexes help find attendance by date, status, and employee. I did not add an index for every possible search because too many indexes can slow down data updates.

## 2. Punch-in Race

If two punch-in requests arrive at the same time, both may try to add a record. The unique index on employee code and date allows only one record to be saved. The first request gets a success response, and the other gets a 409 error because attendance already exists.

## 3. Ties

If employees have the same late minutes at the limit, they get the same rank. My code keeps all employees whose rank is within the limit, so the result may contain more employees than the limit.

## 4. Headcount

The department summary starts with employees, then looks for their attendance records. It keeps employees even when they have no records, so they are still counted.

## 5. Future Change

For 100 times more data, I would check slow queries and improve indexes based on real usage.
