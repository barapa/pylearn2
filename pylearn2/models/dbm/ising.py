"""
Implementation of a densely connected Ising model in the
pylearn2.models.dbm framework
"""
__authors__ = "Ian Goodfellow"
__copyright__ = "Copyright 2012, Universite de Montreal"
__credits__ = ["Ian Goodfellow"]
__license__ = "3-clause BSD"
__maintainer__ = "Ian Goodfellow"

import numpy as np

from collections import OrderedDict

from theano import function
from theano.gof.op import get_debug_values
from theano.sandbox.rng_mrg import MRG_RandomStreams
import theano.tensor as T

from pylearn2.expr.nnet import sigmoid_numpy
from pylearn2.expr.probabilistic_max_pooling import max_pool_channels
from pylearn2.linear.matrixmul import MatrixMul
from pylearn2.models.dbm import HiddenLayer
from pylearn2.models.dbm import VisibleLayer
from pylearn2.space import CompositeSpace
from pylearn2.space import Conv2DSpace
from pylearn2.space import VectorSpace
from pylearn2.utils import sharedX


"""
Note: if h can be -1 or 1, and p(h) = exp(z*h), then
the expected value of h is given by tanh(z), and the
probability that h is 1 is given by sigmoid(2z)

"""

def init_tanh_bias_from_marginals(dataset, use_y = False):
    if use_y:
        X = dataset.y
    else:
        X = dataset.get_design_matrix()
    if not (X.max() == 1):
        raise ValueError("Expected design matrix to consist entirely "
                "of 0s and 1s, but maximum value is "+str(X.max()))
    assert X.min() == -1.

    mean = X.mean(axis=0)

    mean = np.clip(mean, 1e-7, 1-1e-7)

    init_bias = np.arctanh(mean)

    return init_bias

class IsingVisible(VisibleLayer):
    """
    A DBM visible layer consisting of random variables living
    in a VectorSpace, with values in {-1, 1}
    Implements the energy function term
    -b^T h
    """

    def __init__(self,
            nvis,
            bias_from_marginals = None):
        """
            nvis: the dimension of the space
            bias_from_marginals: a dataset, whose marginals are used to
                            initialize the visible biases
        """

        self.__dict__.update(locals())
        del self.self
        # Don't serialize the dataset
        del self.bias_from_marginals

        self.space = VectorSpace(nvis)
        self.input_space = self.space

        origin = self.space.get_origin()

        if bias_from_marginals is None:
            init_bias = np.zeros((nvis,))
        else:
            init_bias = init_tanh_bias_from_marginals(bias_from_marginals)

        self.bias = sharedX(init_bias, 'visible_bias')

    def get_biases(self):
        return self.bias.get_value()

    def set_biases(self, biases, recenter=False):
        self.bias.set_value(biases)
        if recenter:
            assert self.center
            self.offset.set_value(sigmoid_numpy(self.bias.get_value()))

    def upward_state(self, total_state):
        return total_state

    def get_params(self):
        return [self.bias]

    def sample(self, state_below = None, state_above = None,
            layer_above = None,
            theano_rng = None):

        assert state_below is None

        msg = layer_above.downward_message(state_above)

        bias = self.bias

        z = msg + bias

        phi = T.nnet.sigmoid(2. * z)

        rval = theano_rng.binomial(size = phi.shape, p = phi, dtype = phi.dtype,
                       n = 1 )

        return rval * 2. - 1.

    def make_state(self, num_examples, numpy_rng):
        driver = numpy_rng.uniform(0.,1., (num_examples, self.nvis))
        on_prob = sigmoid_numpy(2. * self.bias.get_value())
        sample = 2. * (driver < on_prob) - 1.

        rval = sharedX(sample, name = 'v_sample_shared')

        return rval

    def expected_energy_term(self, state, average, state_below = None, average_below = None):

        assert state_below is None
        assert average_below is None
        assert average in [True, False]
        self.space.validate(state)

        # Energy function is linear so it doesn't matter if we're averaging or not
        rval = -T.dot(state, self.bias)

        assert rval.ndim == 1

        return rval

class IsingHidden(HiddenLayer):
    """

    A hidden layer with h being a vector in {-1, 1}^dim,
    implementing the energy function term

    -v^T Wh -b^T h

    where W and b are parameters of this layer, and v is
    the upward state of the layer below

    """

    def __init__(self,
            dim,
            layer_name,
            irange = None,
            sparse_init = None,
            sparse_stdev = 1.,
            include_prob = 1.0,
            init_bias = 0.,
            W_lr_scale = None,
            b_lr_scale = None,
            max_col_norm = None):
        """

            include_prob: probability of including a weight element in the set
                    of weights initialized to U(-irange, irange). If not included
                    it is initialized to 0.

        """
        self.__dict__.update(locals())
        del self.self

        self.b = sharedX( np.zeros((self.dim,)) + init_bias, name = layer_name + '_b')

    def get_lr_scalers(self):

        if not hasattr(self, 'W_lr_scale'):
            self.W_lr_scale = None

        if not hasattr(self, 'b_lr_scale'):
            self.b_lr_scale = None

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space

        if isinstance(space, VectorSpace):
            self.requires_reformat = False
            self.input_dim = space.dim
        else:
            self.requires_reformat = True
            self.input_dim = space.get_total_dimension()
            self.desired_space = VectorSpace(self.input_dim)

        self.output_space = VectorSpace(self.dim)

        rng = self.dbm.rng
        if self.irange is not None:
            assert self.sparse_init is None
            W = rng.uniform(-self.irange,
                                 self.irange,
                                 (self.input_dim, self.dim)) * \
                    (rng.uniform(0.,1., (self.input_dim, self.dim))
                     < self.include_prob)
        else:
            assert self.sparse_init is not None
            W = np.zeros((self.input_dim, self.dim))
            W *= self.sparse_stdev

        W = sharedX(W)
        W.name = self.layer_name + '_W'

        self.transformer = MatrixMul(W)

        W ,= self.transformer.get_params()
        assert W.name is not None

    def censor_updates(self, updates):

        if self.max_col_norm is not None:
            W, = self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                col_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=0))
                desired_norms = T.clip(col_norms, 0, self.max_col_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + col_norms))

    def get_total_state_space(self):
        return VectorSpace(self.dim)

    def get_params(self):
        assert self.b.name is not None
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        assert self.b not in rval
        rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def get_weights(self):
        if self.requires_reformat:
            # This is not really an unimplemented case.
            # We actually don't know how to format the weights
            # in design space. We got the data in topo space
            # and we don't have access to the dataset
            raise NotImplementedError()
        W ,= self.transformer.get_params()
        return W.get_value()

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases, recenter = False):
        self.b.set_value(biases)
        if recenter:
            assert self.center
            if self.pool_size != 1:
                raise NotImplementedError()
            self.offset.set_value(sigmoid_numpy(self.b.get_value()))

    def get_biases(self):
        return self.b.get_value()

    def get_weights_format(self):
        return ('v', 'h')

    def get_weights_topo(self):

        if not isinstance(self.input_space, Conv2DSpace):
            raise NotImplementedError()

        W ,= self.transformer.get_params()

        W = W.T

        W = W.reshape((self.detector_layer_dim, self.input_space.shape[0],
            self.input_space.shape[1], self.input_space.nchannels))

        W = Conv2DSpace.convert(W, self.input_space.axes, ('b', 0, 1, 'c'))

        return function([], W)()

    def upward_state(self, total_state):
        return total_state

    def downward_state(self, total_state):
        return total_state

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 2

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=1))
        col_norms = T.sqrt(sq_W.sum(axis=0))

        return OrderedDict([
              ('row_norms_min'  , row_norms.min()),
              ('row_norms_mean' , row_norms.mean()),
              ('row_norms_max'  , row_norms.max()),
              ('col_norms_min'  , col_norms.min()),
              ('col_norms_mean' , col_norms.mean()),
              ('col_norms_max'  , col_norms.max()),
            ])

    def get_monitoring_channels_from_state(self, state):

        P = state

        rval = OrderedDict()

        vars_and_prefixes = [ (P,'') ]

        for var, prefix in vars_and_prefixes:
            v_max = var.max(axis=0)
            v_min = var.min(axis=0)
            v_mean = var.mean(axis=0)
            v_range = v_max - v_min

            # max_x.mean_u is "the mean over *u*nits of the max over e*x*amples"
            # The x and u are included in the name because otherwise its hard
            # to remember which axis is which when reading the monitor
            # I use inner.outer rather than outer_of_inner or something like that
            # because I want mean_x.* to appear next to each other in the alphabetical
            # list, as these are commonly plotted together
            for key, val in [
                    ('max_x.max_u', v_max.max()),
                    ('max_x.mean_u', v_max.mean()),
                    ('max_x.min_u', v_max.min()),
                    ('min_x.max_u', v_min.max()),
                    ('min_x.mean_u', v_min.mean()),
                    ('min_x.min_u', v_min.min()),
                    ('range_x.max_u', v_range.max()),
                    ('range_x.mean_u', v_range.mean()),
                    ('range_x.min_u', v_range.min()),
                    ('mean_x.max_u', v_mean.max()),
                    ('mean_x.mean_u', v_mean.mean()),
                    ('mean_x.min_u', v_mean.min())
                    ]:
                rval[prefix+key] = val

        return rval

    def sample(self, state_below = None, state_above = None,
            layer_above = None,
            theano_rng = None):

        if theano_rng is None:
            raise ValueError("theano_rng is required; it just defaults to None so that it may appear after layer_above / state_above in the list.")

        if state_above is not None:
            msg = layer_above.downward_message(state_above)
        else:
            msg = None

        if self.requires_reformat:
            state_below = self.input_space.format_as(state_below, self.desired_space)

        z = self.transformer.lmul(state_below) + self.b

        if msg != None:
            z = z + msg

        on_prob = T.nnet.sigmoid(2. * z)

        samples = theano_rng.binomial(p = on_prob, n=1, size=on_prob.shape, dtype=on_prob.dtype) * 2. - 1.

        return samples

    def downward_message(self, downward_state):
        rval = self.transformer.lmul_T(downward_state)

        if self.requires_reformat:
            rval = self.desired_space.format_as(rval, self.input_space)

        return rval

    def init_mf_state(self):
        raise NotImplementedError("This is just a copy-paste of BVMP")
        # work around theano bug with broadcasted vectors
        z = T.alloc(0., self.dbm.batch_size, self.detector_layer_dim).astype(self.b.dtype) + \
                self.b.dimshuffle('x', 0)
        rval = max_pool_channels(z = z,
                pool_size = self.pool_size)
        return rval

    def make_state(self, num_examples, numpy_rng):
        """ Returns a shared variable containing an actual state
           (not a mean field state) for this variable.
        """
        driver = numpy_rng.uniform(0.,1., (num_examples, self.dim))
        on_prob = sigmoid_numpy(2. * self.b.get_value())
        sample = 2. * (driver < on_prob) - 1.

        rval = sharedX(sample, name = 'v_sample_shared')

        return rval

    def expected_energy_term(self, state, average, state_below, average_below):

        # state = Print('h_state', attrs=['min', 'max'])(state)

        self.input_space.validate(state_below)

        if self.requires_reformat:
            if not isinstance(state_below, tuple):
                for sb in get_debug_values(state_below):
                    if sb.shape[0] != self.dbm.batch_size:
                        raise ValueError("self.dbm.batch_size is %d but got shape of %d" % (self.dbm.batch_size, sb.shape[0]))
                    assert reduce(lambda x,y: x * y, sb.shape[1:]) == self.input_dim

            state_below = self.input_space.format_as(state_below, self.desired_space)

        # Energy function is linear so it doesn't matter if we're averaging or not
        # Specifically, our terms are -u^T W d - b^T d where u is the upward state of layer below
        # and d is the downward state of this layer

        bias_term = T.dot(state, self.b)
        weights_term = (self.transformer.lmul(state_below) * state).sum(axis=1)

        rval = -bias_term - weights_term

        assert rval.ndim == 1

        return rval

    def linear_feed_forward_approximation(self, state_below):
        """
        Used to implement TorontoSparsity. Unclear exactly what properties of it are
        important or how to implement it for other layers.

        Properties it must have:
            output is same kind of data structure (ie, tuple of theano 2-tensors)
            as mf_update

        Properties it probably should have for other layer types:
            An infinitesimal change in state_below or the parameters should cause the same sign of change
            in the output of linear_feed_forward_approximation and in mf_update

            Should not have any non-linearities that cause the gradient to shrink

            Should disregard top-down feedback
        """

        z = self.transformer.lmul(state_below) + self.b

        if self.pool_size != 1:
            # Should probably implement sum pooling for the non-pooled version,
            # but in reality it's not totally clear what the right answer is
            raise NotImplementedError()

        return z, z

    def mf_update(self, state_below, state_above, layer_above = None, double_weights = False, iter_name = None):

        self.input_space.validate(state_below)

        if self.requires_reformat:
            if not isinstance(state_below, tuple):
                for sb in get_debug_values(state_below):
                    if sb.shape[0] != self.dbm.batch_size:
                        raise ValueError("self.dbm.batch_size is %d but got shape of %d" % (self.dbm.batch_size, sb.shape[0]))
                    assert reduce(lambda x,y: x * y, sb.shape[1:]) == self.input_dim

            state_below = self.input_space.format_as(state_below, self.desired_space)

        if iter_name is None:
            iter_name = 'anon'

        if state_above is not None:
            assert layer_above is not None
            msg = layer_above.downward_message(state_above)
            msg.name = 'msg_from_'+layer_above.layer_name+'_to_'+self.layer_name+'['+iter_name+']'
        else:
            msg = None

        if double_weights:
            state_below = 2. * state_below
            state_below.name = self.layer_name + '_'+iter_name + '_2state'
        z = self.transformer.lmul(state_below) + self.b
        if self.layer_name is not None and iter_name is not None:
            z.name = self.layer_name + '_' + iter_name + '_z'
        if msg is not None:
            z = z + msg
        h = T.tanh(z)

        return h

