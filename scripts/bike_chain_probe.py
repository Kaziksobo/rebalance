import csv
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, TypedDict

type BikeId = str
type StartTimeDiff = int
type EndTimeDiff = int
type StartStationId = str
type EndStationId = str
type JourneyNumber = int


class BikeJourney(NamedTuple):
    start_time_diff: StartTimeDiff
    end_time_diff: EndTimeDiff
    start_station_id: StartStationId
    end_station_id: EndStationId
    journey_number: JourneyNumber


type BikeJourneys = list[BikeJourney]
type BikeJourneysById = defaultdict[BikeId, BikeJourneys]

type BreakStart = int
type BreakEnd = int
type BreakFromStation = str
type BreakToStation = str
type BreakPreviousJourney = int
type BreakNextJourney = int


class Break(NamedTuple):
    start: BreakStart
    end: BreakEnd
    from_station: BreakFromStation
    to_station: BreakToStation
    previous_journey: BreakPreviousJourney
    next_journey: BreakNextJourney


type Breaks = list[Break]

type OverlapPreviousEnd = int
type OverlapNextStart = int
type OverlapStationA = str
type OverlapStationB = str
type OverlapPreviousJourney = int
type OverlapNextJourney = int


class Overlap(NamedTuple):
    previous_end: OverlapPreviousEnd
    next_start: OverlapNextStart
    station_a: OverlapStationA
    station_b: OverlapStationB
    previous_journey: OverlapPreviousJourney
    next_journey: OverlapNextJourney


type Overlaps = list[Overlap]


class BikeStats(TypedDict):
    trips: int
    overlaps: Overlaps
    breaks: Breaks


type BikeStatsById = defaultdict[BikeId, BikeStats]

type GapHours = float
type GapsHours = list[GapHours]
type GapsByDayGap = list[GapsHours]

type DayGapCounts = list[int]

SAME_DAY, OVERNIGHT, TWO_OR_MORE = 0, 1, 2
DAY_GAP_LABELS = ("same day", "overnight", "two or more days")


DATA_DIR_FROM_ARG = len(sys.argv) > 1
DATA_DIR = Path(sys.argv[1]) if DATA_DIR_FROM_ARG else Path("data/raw/trips")
START_DATETIME = datetime.fromisoformat("2026-02-01T00:00:00")
OUTPUT_DIR = Path("outputs")

data_files = sorted(DATA_DIR.glob("*.csv"))

if not data_files:
    raise FileNotFoundError(f"No CSV files found in {DATA_DIR}")

bikes: BikeJourneysById = defaultdict(list)


def pct(numerator: int, denominator: int) -> str:
    """Return numerator / denominator as a percentage, or "n/a" if undefined."""
    return f"{numerator / denominator * 100:.2f}%" if denominator else "n/a"


for data_file in data_files:
    with data_file.open(encoding="utf-8-sig", newline="") as f:
        data = csv.DictReader(f)

        for journey in data:
            bike_id = str(journey["Bike number"])

            start_time = datetime.fromisoformat(journey["Start date"])
            end_time = datetime.fromisoformat(journey["End date"])

            bikes[bike_id].append(
                BikeJourney(
                    start_time_diff=int(
                        (start_time - START_DATETIME).total_seconds() // 60
                    ),
                    end_time_diff=int(
                        (end_time - START_DATETIME).total_seconds() // 60
                    ),
                    start_station_id=str(journey["Start station number"]),
                    end_station_id=str(journey["End station number"]),
                    journey_number=int(journey["Number"]),
                )
            )

for journeys in bikes.values():
    journeys.sort(key=lambda journey: (journey.start_time_diff, journey.end_time_diff))

print(f"Loaded {len(data_files)} files")
print(f"Loaded {len(bikes)} bikes")


def new_bike_stats() -> BikeStats:
    return {"trips": 0, "overlaps": [], "breaks": []}


bike_stats: BikeStatsById = defaultdict(new_bike_stats)

pairs_by_day_gap: DayGapCounts = [0, 0, 0]

for bike_id, journeys in bikes.items():
    stats: BikeStats = bike_stats[bike_id]
    stats["trips"] = len(journeys)

    for i in range(len(journeys) - 1):
        current_journey: BikeJourney = journeys[i]
        next_journey: BikeJourney = journeys[i + 1]

        if (current_journey.end_time_diff - next_journey.start_time_diff) > 0:
            stats["overlaps"].append(
                Overlap(
                    previous_end=current_journey.end_time_diff,
                    next_start=next_journey.start_time_diff,
                    station_a=current_journey.end_station_id,
                    station_b=next_journey.start_station_id,
                    previous_journey=current_journey.journey_number,
                    next_journey=next_journey.journey_number,
                )
            )
        else:
            day_gap: int = (
                next_journey.start_time_diff // 1440
                - current_journey.end_time_diff // 1440
            )
            pairs_by_day_gap[min(day_gap, 2)] += 1

            if current_journey.end_station_id != next_journey.start_station_id:
                stats["breaks"].append(
                    Break(
                        start=current_journey.end_time_diff,
                        end=next_journey.start_time_diff,
                        from_station=current_journey.end_station_id,
                        to_station=next_journey.start_station_id,
                        previous_journey=current_journey.journey_number,
                        next_journey=next_journey.journey_number,
                    )
                )

total_trips: int = sum(stats["trips"] for stats in bike_stats.values())
total_pairs: int = total_trips - len(bikes)
total_overlaps: int = sum(len(stats["overlaps"]) for stats in bike_stats.values())
total_breaks: int = sum(len(stats["breaks"]) for stats in bike_stats.values())

assert sum(pairs_by_day_gap) == total_pairs - total_overlaps

breaks_by_day_gap: DayGapCounts = [0, 0, 0]
gaps_by_day_gap: GapsByDayGap = [[], [], []]

for stats in bike_stats.values():
    for b in stats["breaks"]:
        k = min(b.end // 1440 - b.start // 1440, 2)
        breaks_by_day_gap[k] += 1
        gaps_by_day_gap[k].append((b.end - b.start) / 60)

assert sum(breaks_by_day_gap) == total_breaks

# Short gap breaks (60 mins or less)
short_gap_breaks: int = sum(
    1
    for gaps in gaps_by_day_gap
    for gap in gaps
    if gap <= 1.0  # 1.0 hour = 60 minutes
)


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

if DATA_DIR_FROM_ARG:
    report_filename = "chain_probe_test.txt"
    break_log_filename = "chain_probe_breaks_test.csv"
else:
    report_filename = (
        f"chain_probe_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}.txt"
    )
    break_log_filename = (
        f"chain_probe_breaks_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}.csv"
    )

with (OUTPUT_DIR / report_filename).open("w", encoding="utf-8-sig") as f:
    f.write(
        f"Bike chain probe report generated on {datetime.now(tz=UTC).isoformat()}\n"
    )
    f.write(f"Report generated by script: {Path(__file__).resolve()}\n")
    f.write(f"Data files: {', '.join(str(path) for path in data_files)}\n")
    f.write(f"Break log file: {break_log_filename}\n")
    f.write(f"No. of files: {len(data_files)}\n")
    f.write(f"No. of trips: {total_trips}\n")
    f.write(f"No. of bikes: {len(bikes)}\n")
    f.write("\n")
    f.write(f"No. of pairs: {total_pairs}\n")
    f.write(f"No. of overlaps: {total_overlaps}\n")
    f.write(f"Overlap share: {pct(total_overlaps, total_pairs)}\n")
    f.write(f"No. of breaks: {total_breaks}\n")
    f.write(f"Break rate: {pct(total_breaks, total_pairs - total_overlaps)}\n")
    f.write(f"No. of consistent pairs: {total_pairs - total_overlaps - total_breaks}\n")
    f.write("\n")
    f.write("Break rate by gap class\n")
    for k, label in enumerate(DAY_GAP_LABELS):
        pairs = pairs_by_day_gap[k]
        breaks = breaks_by_day_gap[k]
        rate = f"{breaks / pairs * 100:.2f}%" if pairs else "n/a"
        f.write(f"{label}: {pairs:,} pairs, {breaks:,} breaks, break rate {rate}\n")
    f.write("\n")
    f.write(
        f"No. of short gap breaks (60 mins or less): {short_gap_breaks} "
        f"({pct(short_gap_breaks, total_breaks)} of all breaks)\n"
    )
    f.write("\n")
    f.write("10 bikes with the most breaks:\n")
    for i, (bike, stats) in enumerate(
        sorted(bike_stats.items(), key=lambda x: len(x[1]["breaks"]), reverse=True)[
            :10
        ],
        start=1,
    ):
        f.write(
            f"{i}. Bike {bike}: {len(stats['breaks'])} "
            f"breaks in {stats['trips']} trips\n"
        )
    f.write("\n")
    population = [
        (bike, break_)
        for bike, stats in bike_stats.items()
        for break_ in stats["breaks"]
    ]
    k = min(5, len(population))
    f.write(f"{k} example breaks:\n")
    random.seed(42)  # for reproducibility
    random_breaks = random.sample(
        [
            (bike, break_)
            for bike, stats in bike_stats.items()
            for break_ in stats["breaks"]
        ],
        k=k,
    )
    for i, (bike, break_) in enumerate(random_breaks, start=1):
        time_parked = (START_DATETIME + timedelta(minutes=break_.start)).strftime(
            "%Y-%m-%d %H:%M"
        )
        time_taken = (START_DATETIME + timedelta(minutes=break_.end)).strftime(
            "%Y-%m-%d %H:%M"
        )
        f.write(
            f"{i}. Bike {bike}, parked at {break_.from_station} at "
            f"{time_parked}, "
            f"taken from {break_.to_station} at "
            f"{time_taken}\n"
        )

print(f"Report written to {OUTPUT_DIR / report_filename}")

with (OUTPUT_DIR / break_log_filename).open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(
        [
            "bike_id",
            "previous_journey_number",
            "next_journey_number",
            "parked_datetime",
            "taken_datetime",
            "from_station",
            "to_station",
            "gap_minutes",
            "gap_class",
        ]
    )
    for bike, stats in bike_stats.items():
        for break_ in stats["breaks"]:
            parked_datetime = START_DATETIME + timedelta(minutes=break_.start)
            taken_datetime = START_DATETIME + timedelta(minutes=break_.end)
            gap_minutes = break_.end - break_.start
            gap_class = min(break_.end // 1440 - break_.start // 1440, 2)
            writer.writerow(
                [
                    bike,
                    break_.previous_journey,
                    break_.next_journey,
                    parked_datetime.isoformat(),
                    taken_datetime.isoformat(),
                    break_.from_station,
                    break_.to_station,
                    gap_minutes,
                    DAY_GAP_LABELS[gap_class],
                ]
            )

print(f"Break log written to {OUTPUT_DIR / break_log_filename}")
