#!/bin/bash

counter=1
max_counter=20

until [ $counter -gt $max_counter ]
do
  echo Counter: $counter / $max_counter
  python analysis/analyze_threshold.py
  ((counter++))
done
