# Automatic Documentation

## Getting Started 
1. Load the data in by unzipping your Bonaparte zip file into the data directory `cp -r your_bonparte_dir/* project/data/`
2. Clean and prep the raw data `python -m project.utils.preprocess --run-all`
3. You are ready to go


## Running Models:
All models can be run by installing requirements, and calling `python -m project.models.the_model_of_interest -h`, which will print the help.
