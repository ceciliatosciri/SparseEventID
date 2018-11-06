#!/usr/bin/env python
import yaml
import sys

# import all of the possible trainers:
from network import trainercore


def main(params):

    trainer = trainercore.trainercore(params)

    # Create the network and set it in the trainer:

    trainer.initialize()
    trainer.batch_process()


if __name__ == '__main__':

    if len(sys.argv) < 2:
        sys.stdout.write('Requires configuration file.  [train.py config.yml]\n')
        sys.stdout.flush()
        exit()

    config = sys.argv[-1]

    with open(config, 'r') as f:
        params = yaml.load(f)

    main(params)
