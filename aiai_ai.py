from enum import Enum
import logging
from optparse import OptionParser
from os import listdir
from pickle import dump
from time import perf_counter, sleep
from warnings import filterwarnings
from functools import partial

import cv2
import neat
import numpy as np
from pyautogui import screenshot
from PIL import ImageGrab
from skimage.metrics import structural_similarity as compare_ssim
from torch import hub

from controller import Controller


class LogOptions(Enum):
    FULL = 'full'
    PARTIAL = 'partial'
    NONE = 'none'


def create_logger(option):
    log = logging.getLogger("Aiai_AI")
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    log.propagate = False

    if option != LogOptions.NONE:
        console_handle = logging.StreamHandler()
        console_handle.setLevel(logging.DEBUG)

        log_format = logging.Formatter('%(levelname)s: %(message)s')
        console_handle.setFormatter(log_format)

        log.addHandler(console_handle)
    return log


def get_img():
    img = screenshot(region=(x_pad, y_pad, width, height))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def img_similarity(img, compare, shape, threshold=0.75):
    crop = img[shape[1]:shape[1] + compare.shape[0], shape[0]:shape[0] + compare.shape[1]]

    # Chroma key
    mask = cv2.inRange(compare, rgb_low, rgb_up)
    com_copy, crop_copy = np.copy(compare), np.copy(crop)

    com_copy = compare - cv2.bitwise_and(com_copy, com_copy, mask=mask)
    crop_copy = crop - cv2.bitwise_and(crop_copy, crop_copy, mask=mask)

    # Convert to grayscale
    crop_copy = cv2.cvtColor(crop_copy, cv2.COLOR_BGR2GRAY)
    com_copy = cv2.cvtColor(com_copy, cv2.COLOR_BGR2GRAY)

    return compare_ssim(com_copy, crop_copy) > threshold


# TODO Bottle neck
def detect_goal(img):
    g = -25
    ref = model(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), size=640)
    results = ref.xyxy[0]
    if len(results) > 0:
        x1, y1, x2, y2, prob, _ = results[0]
        if prob > 0.55:
            x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
            g = min((((x2 - x1) * (y2 - y1)) / (width * height)) * 125, 50)
    return g


def interpret_and_act(img, x_input, y_input, st, g_max):
    done, info = False, ''

    controller.do_movement(x_input, y_input)

    g_max = max(g_max, detect_goal(img))

    if img_similarity(img, time_over, to_shape):
        g_max -= 25  # [-25, 25]
        done, info = True, 'Time Over'
    elif img_similarity(img, fall_out, fo_shape):
        g_max -= 50  # [-50, 0]
        done, info = True, 'Fall Out'
    elif img_similarity(img, goal, g_shape):
        g_max = 30 + (1.25 * (60 - (perf_counter() - st)))  # [30, 105]
        done, info = True, 'Goal'

    return g_max, done, info


def conduct_genome(genome, cfg, genome_id, pop=None):
    global p

    p = p if pop is None else pop

    net = neat.nn.recurrent.RecurrentNetwork.create(genome, cfg)

    sleep(2.5)  # Allow time to load up

    current_max_fitness, g_max, step, zero_step, done = 0, 0, 0, 0, False

    controller.load_state()
    if options.logging == LogOptions.FULL:
        logger.info(f'running genome {genome_id} in generation {p.generation}')
    st = perf_counter()
    while not done:
        # TODO Consistent intervals (investigate further) or pause during comp
        # get next image
        img = get_img()

        img_copy = cv2.resize(img, (inx, iny))
        img_copy = np.reshape(img_copy, (inx, iny, inc))

        img_array = np.ndarray.flatten(img_copy)

        # Get end result input to game
        x_input, y_input = net.activate(img_array)

        g_max, done, info = interpret_and_act(img, x_input, y_input, st, g_max)

        if info != '' and options.logging == LogOptions.FULL:
            logger.info(f'{info}')

        if g_max > current_max_fitness:
            current_max_fitness = g_max
            step, zero_step = 0, 0
        elif img_similarity(img, zero_mph, zm_shape, threshold=0.94):
            zero_step += 60
        else:
            step += 1
            zero_step = 0
        if done or step > max_steps or zero_step > max_steps:
            done = True
            if step > max_steps or zero_step > max_steps:
                if options.logging == LogOptions.FULL:
                    logger.info('Timed out due to stagnation')
                g_max -= 25
            logger.info(f'generation: {p.generation}, genome: {genome_id}, fitness: {g_max}')
        genome.fitness = g_max
    controller.do_movement(0, 0)  # Reset movement
    return genome.fitness


def update_stats(gen, sr, file='stats.csv'):
    with open(file, 'a') as f:
        f.write(','.join([str(gen), str(max_fitness[gen]), str(sr.get_fitness_mean()[-1]),
                          str(sr.get_fitness_stdev()[-1])]) + '\n')


def eval_genomes(genomes, cfg):
    if len(stat_reporter.get_fitness_mean()) > 0 and options.stats:
        update_stats(p.generation - 1, stat_reporter)
    max_fit = -50
    for genome_id, genome in genomes:
        fit = conduct_genome(genome, cfg, genome_id)
        max_fit = max(max_fit, fit)
    max_fitness[p.generation] = max_fit


# Controller
controller = Controller()

# Image setup
ImageGrab.grab = partial(ImageGrab.grab, all_screens=True)
width, height, x_pad, y_pad, scale = 1300, 1000, 310, 30, 25
inx, iny, inc = width // scale, height // scale, 3
rgb_low, rgb_up = np.array([0, 10, 0]), np.array([120, 255, 100])

# Reference images
time_over = cv2.imread('images/time_over.png')
to_x_pad, to_y_pad = 405, 460
to_shape = (to_x_pad - x_pad, to_y_pad - y_pad)

goal = cv2.imread('images/goal.png')
g_x_pad, g_y_pad = 700, 635
g_shape = (g_x_pad - x_pad, g_y_pad - y_pad)

fall_out = cv2.imread('images/fall_out.png')
fo_x_pad, fo_y_pad = 430, 445
fo_shape = (fo_x_pad - x_pad, fo_y_pad - y_pad)

zero_mph = cv2.imread('images/zeromph.png')
zm_x_pad, zm_y_pad = 410, 880
zm_shape = (zm_x_pad - x_pad, zm_y_pad - y_pad)

# Goal detection
filterwarnings("ignore", category=UserWarning)
filterwarnings("ignore", category=RuntimeWarning)
model = hub.load('yolov5', 'custom', 'yolov5/runs/train/exp/weights/best.pt', source='local')

max_steps = 500
max_fitness = {}

if __name__ == '__main__':
    parser = OptionParser()

    parser.add_option('-l', '--logging', dest='logging', choices=[o for o in LogOptions],
                      help='Logging options: [full, partial, none]. (Default=full)', default=LogOptions.FULL)
    parser.add_option('-s', '--stats', dest='stats', help='Argument for saving evolution stats. (Default=true)',
                      action='store_true', default=True)

    options, args = parser.parse_args()

    logger = create_logger(options.logging)

    # Network setup
    checkpointer = neat.Checkpointer(generation_interval=1, filename_prefix='history/neat-checkpoint-')
    config = neat.Config(neat.DefaultGenome, neat.DefaultReproduction, neat.DefaultSpeciesSet, neat.DefaultStagnation,
                         'config-feedforward')
    if len(listdir('history')) > 0:
        m = max([int(f[f.rfind('-') + 1:]) for f in listdir('history')])
        p = checkpointer.restore_checkpoint(f'history/neat-checkpoint-{m}')
        p.generation += 1
        logger.info(f'Restoring checkpoint {m}')
        p.config = config
    else:
        p = neat.Population(config)
    p.add_reporter(neat.StdOutReporter(True))
    stat_reporter = neat.StatisticsReporter()
    p.add_reporter(stat_reporter)
    p.add_reporter(checkpointer)

    # Final
    winner = p.run(eval_genomes)

    with open('winner.pkl', 'wb') as output:
        dump(winner, output, 1)
