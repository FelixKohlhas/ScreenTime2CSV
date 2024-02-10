# ScreenTime2CSV

ScreenTime2CSV is a Python script to query Screen Time data from the macOS knowledgeC.db database and export it to CSV format.

## Requirements for reading iOS Screen Time
- MacOS device signed into the same iCloud account
- Screen Time "Share across devices" enabled

More info in my blog post [Exporting and analyzing iOS Screen Time usage](https://felixkohlhas.com/projects/screentime/) 

## Usage

```
usage: screentime2csv.py [-h] [-o OUTPUT] [-d DELIMITER]

Query knowledge database

options:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        Output file path
  -d DELIMITER, --delimiter DELIMITER
                        Delimiter for output file (default: comma)
```

## Example
```bash
python ScreenTime2CSV.py -o output.csv
```
This command will export Screen Time data to output.csv using comma as the delimiter.