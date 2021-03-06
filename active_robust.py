import torch
import argparse
import json
import os
import yaml
import numpy as np
from tqdm import tqdm
import time
from numpyencoder import NumpyEncoder

from src.training_manager import TrainPipeline
from src.model_manager import flatten_grads, dist_grads_to_model

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = True


class ActiveSamplingRobust(TrainPipeline):
    """
        This Pipeline implements robust SGD + various online active sample selection methods
    """
    def __init__(self, config, seed):
        TrainPipeline.__init__(self, config=config, seed=seed)

    def run_train(self):
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)

    def run_batch_train(self):
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)

        while self.epoch < self.num_epochs:
            self.model.to(device)
            self.model.train()
            epoch_grad_cost = 0
            epoch_agg_cost = 0
            epoch_gm_iter = 0
            epoch_compression_cost = 0

            # ------- Training Phase --------- #
            print('epoch {}/{} || learning rate: {}'.format(self.epoch,
                                                            self.num_epochs,
                                                            self.optimizer.param_groups[0]['lr']))
            p_bar = tqdm(total=len(self.train_loader))
            p_bar.set_description("Training Progress: ")

            for batch_ix, (images, labels) in enumerate(self.train_loader):
                self.metrics["num_iter"] += 1
                t_iter = time.time()

                # Forward Pass
                images = images.to(device)
                labels = labels.to(device)
                outputs = self.model(images)
                self.optimizer.zero_grad()
                loss = self.criterion(outputs, labels)

                # compute grad
                loss.backward()
                self.metrics["num_grad_steps"] += 1
                # Note: No Optimizer Step yet.
                g_i = flatten_grads(learner=self.model)
                # Construct the Jacobian
                if self.G is None:
                    d = len(g_i)
                    print("Num of Parameters {}".format(d))
                    self.metrics["num_param"] = d
                    self.G = np.zeros((self.num_batches, d), dtype=g_i.dtype)

                ix = batch_ix % self.num_batches
                agg_ix = (batch_ix + 1) % self.num_batches
                self.G[ix, :] = g_i
                iteration_time = time.time() - t_iter
                epoch_grad_cost += iteration_time

                p_bar.update()

                if agg_ix == 0 and batch_ix != 0:
                    lr = self.optimizer.param_groups[0]['lr']
                    if self.C_J is not None:
                        t0 = time.time()
                        self.I_k = self.C_J.compress(G=self.G, lr=lr)
                        epoch_compression_cost += time.time() - t0
                        self.metrics["jacobian_residual"].append(self.C_J.normalized_residual)
                        # Gradient aggregation - get aggregated gradient vector
                        agg_g = self.gar.aggregate(G=self.C_J.G_sparse,
                                                   ix=self.I_k,
                                                   axis=self.C_J.axis)
                    else:
                        agg_g = self.gar.aggregate(G=self.G, ix=self.I_k, axis=0)

                    epoch_gm_iter += self.gar.num_iter
                    epoch_agg_cost += self.gar.agg_time
                    # Reset GAR stats
                    self.gar.agg_time = 0
                    self.gar.num_iter = 0

                    # Update Model Grads with aggregated g : i.e. compute \tilde(g)
                    self.optimizer.zero_grad()
                    dist_grads_to_model(grads=agg_g, learner=self.model)
                    self.model.to(device)

                    # Now Do an optimizer step with x_t+1 = x_t - \eta \tilde(g)
                    self.optimizer.step()
                    self.metrics["num_opt_steps"] += 1

                self.metrics["epoch_grad_cost"].append(epoch_grad_cost)
                self.metrics["epoch_agg_cost"].append(epoch_agg_cost)
                if epoch_gm_iter > 0:
                    self.metrics["epoch_gm_iter"].append(epoch_gm_iter)
                if epoch_compression_cost > 0:
                    # print("Epoch Sparse Approx Cost: {}".format(epoch_sparse_cost))
                    self.metrics["epoch_compression_cost"].append(epoch_compression_cost)

            train_loss = self.evaluate_classifier(model=self.model,
                                                  train_loader=self.train_loader,
                                                  test_loader=self.test_loader,
                                                  metrics=self.metrics,
                                                  device=device,
                                                  epoch=self.epoch,
                                                  num_epochs=self.num_epochs)
            # Stop if diverging
            if (train_loss > 1e3) | np.isnan(train_loss) | np.isinf(train_loss):
                self.epoch = self.num_epochs

            self.epoch += 1
            if self.lrs is not None:
                self.lrs.step()
            # Update Total Complexities
            self.metrics["total_grad_cost"] = sum(self.metrics["epoch_grad_cost"])
            self.metrics["total_agg_cost"] = sum(self.metrics["epoch_agg_cost"])
            self.metrics["total_gm_iter"] = sum(self.metrics["epoch_gm_iter"])
            self.metrics["total_compression_cost"] = sum(self.metrics["epoch_compression_cost"])
            self.metrics["total_cost"] = self.metrics["total_grad_cost"] + self.metrics["total_agg_cost"] + \
                                         self.metrics[
                                             "total_compression_cost"]
            if self.metrics["total_gm_iter"] != 0:
                # Handle Non GM GARs
                self.metrics["avg_gm_cost"] = self.metrics["total_agg_cost"] / self.metrics["total_gm_iter"]

    def run_fed_train(self):
        raise NotImplementedError("This method needs to be implemented for each pipeline")


def _parse_args():
    parser = argparse.ArgumentParser(description='federated/decentralized/distributed training experiment template')
    parser.add_argument('--train_mode',
                        type=str,
                        default='distributed',
                        help='vanilla: launch regular batch sgd'
                             'distributed: launch distributed Training '
                             'fed: launch federated training')
    parser.add_argument('--conf',
                        type=str,
                        default=None,
                        help='Pass Config file path')
    parser.add_argument('--o',
                        type=str,
                        default='default_output',
                        help='Pass result file path')
    parser.add_argument('--dir',
                        type=str,
                        default=None,
                        help='Pass result file dir')
    parser.add_argument('--n_repeat',
                        type=int,
                        default=1,
                        help='Specify number of repeat runs')
    args = parser.parse_args()
    return args


def run_main():
    args = _parse_args()
    print(args)
    root = os.getcwd()
    config_path = args.conf if args.conf else root + '/configs/default_robust_config.yaml'
    config = yaml.load(open(config_path), Loader=yaml.FullLoader)

    # Training - Repeat over the random seeds #
    # ----------------------------------------
    results = []
    for seed in np.arange(args.n_repeat):
        # ----- Launch Training ------ #
        train_mode = args.train_mode
        trainer = ActiveSamplingRobust(config=config, seed=seed)

        if train_mode == 'vanilla':
            # Launch Vanilla mini-batch Training
            trainer.run_train()
        elif train_mode == 'distributed':
            trainer.run_batch_train()
        else:
            raise NotImplementedError
        results.append(trainer.metrics)

    # Write Results #
    # ----------------
    directory = args.dir if args.dir else "result_dumps/"
    if not os.path.exists(directory):
        os.makedirs(directory)
    with open(directory + args.o, 'w+') as f:
        json.dump(results, f, indent=4, ensure_ascii=False, cls=NumpyEncoder)


if __name__ == '__main__':
    run_main()
