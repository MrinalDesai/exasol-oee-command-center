# Problem statement

**Track 03 · Factories, Fleets & Supply Chains**

## Who has the problem

Plant and maintenance managers at mid-sized Indian manufacturers: machine shops,
auto-component makers, packaging lines. They run a few plants and a warehouse, a
few dozen machines each, and a mix of systems bought over the years: a sensor
historian, a PLC log, an energy meter, an ERP for stores, a quality register, and
supplier price lists that arrive as spreadsheets and email.

## What goes wrong

1. **The data is scattered and incomplete.** The same machine has a different name
   in every system. Clocks differ (UTC vs India time), units differ, energy arrives
   hours late in 15-minute blocks, and minutes go missing. Nobody can look at one
   machine across all of it.
2. **Unplanned downtime with no clear root cause.** A machine stops; the team
   guesses (bearing? coolant? overload? drive?) and works through them one by one.
   Each fault does leave a pattern, but it only shows when the streams are read
   together.
3. **Spare parts held in the wrong location.** The part needed is "out of stock" at
   the plant, while another plant or the warehouse has it in a different store
   system that writes the part number differently. The line waits days for the
   supplier.
4. **Quality issues caught at the end of the line.** Bad parts are found at
   inspection, after the shift has already made them.
5. **Suppliers reporting in inconsistent formats.** Three suppliers sell the same
   part under three names, in different currencies, units and file formats. A
   normal search for our part number finds nothing.

## Why it matters

Unplanned downtime is the largest loss in OEE (availability × performance ×
quality). Every hour a line waits for a diagnosis, a part or a re-run is lost
output, overtime and late deliveries. Scrap made before anyone notices costs the
material and the machine time twice. These plants usually cannot afford a data
team or a large platform project, so the answer has to run on one database they
can install themselves.

## What we built

**OEE Command Center** on Exasol Personal. All five problems are solved inside
the database with SQL and Python UDFs, on one copy of the data:

| Problem | What Exasol does | Result |
|---|---|---|
| Scattered data | Combines five sensor systems by time, minute by minute, and fills short gaps | 4.5 M rows into 2.0 M aligned minutes in about 5 s |
| No root cause | Searches the whole history for each fault's five-stream pattern; replays a live machine until the pattern names the fault | 104 of 126 failures (83%) warned across the four faults; the overstrain pattern came true every time |
| Wrong location | Reads four store systems with different layouts as one; flags wrong-location stock and the fastest source | Demo kit on site in 10 h instead of 8 days |
| Quality at end of line | Scores every shift before it starts (Go / Check first / Stop) | Warned before 22 of 46 new quality problems; end-of-line inspection warns before none |
| Supplier formats | Deep scan reads every format, converts units and currency, and judges each match | Builds the order line from catalogs where a plain search finds 0 |

All data in this project is synthetic. See [README.md](README.md) for the demo,
and [RUN_GUIDE.md](RUN_GUIDE.md) to run it.
