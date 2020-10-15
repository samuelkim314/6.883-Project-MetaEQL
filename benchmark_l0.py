"""Trains the deep symbolic regression architecture on given functions to produce a simple equation that describes
the dataset."""

import pickle
import numpy as np
import os
import torch
import torch.nn as nn
import torch.optim as optim
from utils import pretty_print, functions
from utils.symbolic_network import SymbolicNetL0
from utils.regularization import L12Smooth  #, l12_smooth
from inspect import signature
import time
import argparse


N_TRAIN = 256       # Size of training dataset
N_VAL = 100         # Size of validation dataset
DOMAIN = (-1, 1)    # Domain of dataset - range from which we sample x
# DOMAIN = np.array([[0, -1, -1], [1, 1, 1]])   # Use this format if each input variable has a different domain
N_TEST = 100        # Size of test dataset
DOMAIN_TEST = (-2, 2)   # Domain of test dataset - should be larger than training domain to test extrapolation
NOISE_SD = 0        # Standard deviation of noise for training dataset
var_names = ["x", "y", "z"]

# Standard deviation of random distribution for weight initializations.
init_sd_first = 0.1
init_sd_last = 1.0
init_sd_middle = 0.5
# init_sd_first = 0.5
# init_sd_last = 0.5
# init_sd_middle = 0.5
# init_sd_first = 0.1
# init_sd_last = 0.1
# init_sd_middle = 0.1


def generate_data(func, N, range_min=DOMAIN[0], range_max=DOMAIN[1]):
    """Generates datasets."""
    x_dim = len(signature(func).parameters)     # Number of inputs to the function, or, dimensionality of x
    x = (range_max - range_min) * torch.rand([N, x_dim]) + range_min
    y = torch.tensor([[func(*x_i)] for x_i in x])
    return x, y


class Benchmark:
    """Benchmark object just holds the results directory (results_dir) to save to and the hyper-parameters. So it is
    assumed all the results in results_dir share the same hyper-parameters. This is useful for benchmarking multiple
    functions with the same hyper-parameters."""
    def __init__(self, results_dir, n_layers=2, reg_weight=5e-3, learning_rate=1e-2,
                 n_epochs1=10001, n_epochs2=10001):
        """Set hyper-parameters"""
        self.activation_funcs = [
            *[functions.Constant()] * 2,
            *[functions.Identity()] * 4,
            *[functions.Square()] * 4,
            *[functions.Sin()] * 2,
            *[functions.Exp()] * 2,
            *[functions.Sigmoid()] * 2,
            *[functions.Product(1.0)] * 2
        ]

        self.n_layers = n_layers                # Number of hidden layers
        self.reg_weight = reg_weight            # Regularization weight
        self.learning_rate = learning_rate
        self.summary_step = 1000                # Number of iterations at which to print to screen
        self.n_epochs1 = n_epochs1
        self.n_epochs2 = n_epochs2

        if not os.path.exists(results_dir):
            os.makedirs(results_dir)
        self.results_dir = results_dir

        # Save hyperparameters to file
        result = {
            "learning_rate": self.learning_rate,
            "summary_step": self.summary_step,
            "n_epochs1": self.n_epochs1,
            "n_epochs2": self.n_epochs2,
            "activation_funcs_name": [func.name for func in self.activation_funcs],
            "n_layers": self.n_layers,
            "reg_weight": self.reg_weight,
        }
        with open(os.path.join(self.results_dir, 'params.pickle'), "wb+") as f:
            pickle.dump(result, f)

    def benchmark(self, func, func_name, trials):
        """Benchmark the EQL network on data generated by the given function. Print the results ordered by test error.

        Arguments:
            func: lambda function to generate dataset
            func_name: string that describes the function - this will be the directory name
            trials: number of trials to train from scratch. Will save the results for each trial.
        """

        print("Starting benchmark for function:\t%s" % func_name)
        print("==============================================")

        # Create a new sub-directory just for the specific function
        func_dir = os.path.join(self.results_dir, func_name)
        if not os.path.exists(func_dir):
            os.makedirs(func_dir)

        # Train network!
        expr_list, error_test_list = self.train(func, func_name, trials, func_dir)

        # Sort the results by test error (increasing) and print them to file
        # This allows us to easily count how many times it fit correctly.
        error_expr_sorted = sorted(zip(error_test_list, expr_list))     # List of (error, expr)
        error_test_sorted = [x for x, _ in error_expr_sorted]   # Separating out the errors
        expr_list_sorted = [x for _, x in error_expr_sorted]    # Separating out the expr

        fi = open(os.path.join(self.results_dir, 'eq_summary.txt'), 'a')
        fi.write("\n{}\n".format(func_name))
        for i in range(trials):
            fi.write("[%f]\t\t%s\n" % (error_test_sorted[i], str(expr_list_sorted[i])))
        fi.close()

    def train(self, func, func_name='', trials=1, func_dir='results/test'):
        """Train the network to find a given function"""

        x, y = generate_data(func, N_TRAIN)
        # x_val, y_val = generate_data(func, N_VAL)
        x_test, y_test = generate_data(func, N_TEST, range_min=DOMAIN_TEST[0], range_max=DOMAIN_TEST[1])

        # Setting up the symbolic regression network
        x_dim = len(signature(func).parameters)  # Number of input arguments to the function

        # x_placeholder = tf.placeholder(shape=(None, x_dim), dtype=tf.float32)
        width = len(self.activation_funcs)
        n_double = functions.count_double(self.activation_funcs)

        # Arrays to keep track of various quantities as a function of epoch
        loss_list = []          # Total loss (MSE + regularization)
        error_list = []         # MSE
        reg_list = []           # Regularization
        error_test_list = []    # Test error

        error_test_final = []
        eq_list = []

        for trial in range(trials):
            print("Training on function " + func_name + " Trial " + str(trial+1) + " out of " + str(trials))

            # reinitialize for each trial
            net = SymbolicNetL0(self.n_layers, in_dim=1, funcs=self.activation_funcs,
                              initial_weights=[
                                  # kind of a hack for truncated normal
                                  torch.fmod(torch.normal(0, init_sd_first, size=(x_dim, width + n_double)), 2),
                                  torch.fmod(torch.normal(0, init_sd_middle, size=(width, width + n_double)), 2),
                                  torch.fmod(torch.normal(0, init_sd_middle, size=(width, width + n_double)), 2),
                                  torch.fmod(torch.normal(0, init_sd_last, size=(width, 1)), 2)
                              ])

            criterion = nn.MSELoss()
            optimizer = optim.RMSprop(net.parameters(),
                                      lr=self.learning_rate * 10, momentum=0.0)

            # adapative learning rate
            lmbda = lambda epoch: 0.1 * epoch
            scheduler = optim.lr_scheduler.MultiplicativeLR(optimizer, lr_lambda=lmbda)

            for param_group in optimizer.param_groups:
                print("Learning rate: %f" % param_group['lr'])

            loss_val = np.nan
            # Restart training if loss goes to NaN (which happens when gradients blow up)
            while np.isnan(loss_val):
                t0 = time.time()

                # First stage of training, preceded by 0th warmup stage
                for epoch in range(self.n_epochs1 + 2000):
                    inputs, labels = x, y

                    # zero the parameter gradients
                    optimizer.zero_grad()
                    outputs = net(inputs)

                    mse_loss = criterion(outputs, labels)
                    reg_loss = net.get_loss()
                    loss = mse_loss + self.reg_weight * reg_loss

                    loss.backward()
                    optimizer.step()

                    if epoch % self.summary_step == 0:
                        error_val = mse_loss.item()
                        reg_val = reg_loss.item()
                        loss_val = error_val + self.reg_weight * reg_val
                        print("Epoch: %d\tTotal training loss: %f\tReg loss: %f" % (epoch, loss_val, reg_val))
                        error_list.append(error_val)
                        reg_list.append(reg_val)
                        loss_list.append(loss_val)

                        # TODO: error test val
                        # loss_val, error_val, reg_val, = sess.run((loss, error, reg_loss), feed_dict=feed_dict)
                        # error_test_val = sess.run(error_test, feed_dict={x_placeholder: x_test})
                        # print("Epoch: %d\tTotal training loss: %f\tTest error: %f" % (i, loss_val, error_test_val))

                        # error_list.append(error_val)
                        # error_test_list.append(error_test_val)
                        if np.isnan(loss_val):  # If loss goes to NaN, restart training
                            break

                    if epoch == 2000:
                        scheduler.step()  # lr /= 10
                        for param_group in optimizer.param_groups:
                            print(param_group['lr'])

                # scheduler.step()  # lr /= 10 again
                for param_group in optimizer.param_groups:
                    print("Learning rate: %f" % param_group['lr'])

                # # Masked network - weights below a threshold are set to 0 and frozen. This is the fine-tuning stage
                # sym_masked = MaskedSymbolicNet(sess, sym)
                # y_hat_masked = sym_masked(x_placeholder)
                # error_masked = tf.losses.mean_squared_error(labels=y, predictions=y_hat_masked)
                # error_test_masked = tf.losses.mean_squared_error(labels=y_test, predictions=y_hat_masked)
                # train_masked = opt.minimize(error_masked)

                for epoch in range(self.n_epochs2):
                    inputs, labels = x, y

                    # zero the parameter gradients
                    optimizer.zero_grad()
                    # forward + backward + optimize
                    outputs = net(inputs)

                    mse_loss = criterion(outputs, labels)
                    reg_loss = net.get_loss()
                    loss = mse_loss + self.reg_weight * reg_loss

                    loss.backward()
                    optimizer.step()

                    if epoch % self.summary_step == 0:
                        error_val = mse_loss.item()
                        reg_val = reg_loss.item()
                        loss_val = error_val + self.reg_weight * reg_val
                        print("Epoch: %d\tTotal training loss: %f\tReg loss: %f" % (epoch, loss_val, reg_val))
                        error_list.append(error_val)
                        reg_list.append(reg_val)
                        loss_list.append(loss_val)

                        # TODO: error test val

                        if np.isnan(loss_val):  # If loss goes to NaN, restart training
                            break

                t1 = time.time()

            tot_time = t1 - t0
            print(tot_time)

            # Print the expressions
            with torch.no_grad():
                weights = net.get_weights()
                expr = pretty_print.network(weights, self.activation_funcs, var_names[:x_dim])
                print(expr)

            # Save results
            trial_file = os.path.join(func_dir, 'trial%d.pickle' % trial)
            results = {
                "weights": weights,
                "loss_list": loss_list,
                "error_list": error_list,
                "reg_list": reg_list,
                "error_test": error_test_list,
                "expr": expr,
                "runtime": tot_time
            }
            with open(trial_file, "wb+") as f:
                pickle.dump(results, f)

            # error_test_final.append(error_test_list[-1])
            eq_list.append(expr)

        return eq_list, error_test_final


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Train the EQL network.")
    parser.add_argument("--results-dir", type=str, default='results/benchmark/test')
    parser.add_argument("--n-layers", type=int, default=2, help="Number of hidden layers, L")
    parser.add_argument("--reg-weight", type=float, default=5e-3, help='Regularization weight, lambda')
    parser.add_argument('--learning-rate', type=float, default=1e-2, help='Base learning rate for training')
    parser.add_argument("--n-epochs1", type=int, default=10001, help="Number of epochs to train the first stage")
    parser.add_argument("--n-epochs2", type=int, default=10001,
                        help="Number of epochs to train the second stage, after freezing weights.")

    args = parser.parse_args()
    kwargs = vars(args)
    print(kwargs)

    if not os.path.exists(kwargs['results_dir']):
        os.makedirs(kwargs['results_dir'])
    meta = open(os.path.join(kwargs['results_dir'], 'args.txt'), 'a')
    import json
    meta.write(json.dumps(kwargs))
    meta.close()

    bench = Benchmark(**kwargs)

    bench.benchmark(lambda x: x, func_name="x", trials=10)
    # bench.benchmark(lambda x: x**2, func_name="x^2", trials=20)
    # bench.benchmark(lambda x: x**3, func_name="x^3", trials=20)
    # bench.benchmark(lambda x: np.sin(2*np.pi*x), func_name="sin(2pix)", trials=20)
    # bench.benchmark(lambda x: np.exp(x), func_name="e^x", trials=20)
    # bench.benchmark(lambda x, y: x*y, func_name="xy", trials=20)
    # bench.benchmark(lambda x, y: np.sin(2 * np.pi * x) + np.sin(4*np.pi * y),
    #                 func_name="sin(2pix)+sin(4py)", trials=20)
    # bench.benchmark(lambda x, y, z: 0.5*x*y + 0.5*z, func_name="0.5xy+0.5z", trials=20)
    # bench.benchmark(lambda x, y, z: x**2 + y - 2*z, func_name="x^2+y-2z", trials=20)
    # bench.benchmark(lambda x: np.exp(-x**2), func_name="e^-x^2", trials=20)
    # bench.benchmark(lambda x: 1 / (1 + np.exp(-10*x)), func_name="sigmoid(10x)", trials=20)
    # bench.benchmark(lambda x, y: x**2 + np.sin(2*np.pi*y), func_name="x^2+sin(2piy)", trials=20)

    # 3-layer functions
    # bench.benchmark(lambda x, y, z: (x+y*z)**3, func_name="(x+yz)^3", trials=20)


