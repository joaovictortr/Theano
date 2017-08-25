"""
Concrete spatial transformer implementation
"""
from __future__ import absolute_import, print_function, division

import theano
from theano.tensor import as_tensor_variable
# Instantiate abstract Op, so the user just have to import this module
from .abstract_spatialtf import (AbstractTransformerGrid, AbstractTransformerSampler)


def spatialtf(inp, theta, scale_height=1, scale_width=1, border_mode='nearest'):
    inp = as_tensor_variable(inp)
    assert inp.ndim == 4

    theta = as_tensor_variable(theta)
    assert theta.ndim == 3

    num_batch, num_channels, height, width = inp.shape
    out_height = theano.tensor.cast(scale_height * height, 'int64')
    out_width = theano.tensor.cast(scale_width * width, 'int64')

    out_dims = (num_batch, num_channels, out_height, out_width)
    grid = AbstractTransformerGrid()(theta, out_dims)
    sampler = AbstractTransformerSampler()(inp, grid)
    return sampler


def transformer_grid_impl(theta, out_dims):
    def _linspace(start, stop, num):
        # Theano linspace. Behaves similar to np.linspace
        start = theano.tensor.cast(start, theano.config.floatX)
        stop = theano.tensor.cast(stop, theano.config.floatX)
        num = theano.tensor.cast(num, theano.config.floatX)
        step = (stop - start) / (num - 1)
        return theano.tensor.arange(num, dtype=theano.config.floatX) * step + start

    def _meshgrid(height, width):
        x_t = theano.tensor.dot(theano.tensor.ones((height, 1)),
                                _linspace(-1.0, 1.0, width).dimshuffle('x', 0))
        y_t = theano.tensor.dot(_linspace(-1.0, 1.0, height).dimshuffle(0, 'x'),
                                theano.tensor.ones((1, width)))

        x_t_flat = x_t.reshape((1, -1))
        y_t_flat = y_t.reshape((1, -1))
        ones = theano.tensor.ones_like(x_t_flat)
        grid = theano.tensor.concatenate([x_t_flat, y_t_flat, ones], axis=0)
        return grid

    num_batch, _, out_height, out_width = out_dims
    grid = _meshgrid(out_height, out_width)
    # transform a x (x_t, y_t, 1)^t -> (x_s, y_s)
    transformed_grid = theano.tensor.dot(theta, grid)
    # dimshuffle grid into (2, num_batch, out_height * out_width)
    transposed_grid = transformed_grid.dimshuffle(1, 0, 2)
    # reshape into (2, num_batch, out_height, out_width)
    return transposed_grid.reshape((2, num_batch, out_height, out_width))


def transformer_sampler_impl(inp, grid, border_mode):
    num_batch, num_channels, height, width = inp.shape
    out_height, out_width = grid.shape[2], grid.shape[3]

    height_f = theano.tensor.cast(height, theano.config.floatX)
    width_f = theano.tensor.cast(width, theano.config.floatX)

    inp_transposed = inp.dimshuffle(0, 2, 3, 1)

    # Scale coordinates from [-1, 1] to [0, dimension -1], where dimension
    # can be the width or height
    x = grid[0, :].flatten()
    x = (x + 1) / 2 * (width_f - 1)

    y = grid[1, :].flatten()
    y = (y + 1) / 2 * (height_f - 1)

    # Obtain indices of the 2x2 pixel neighborhood surrounding the coordinates;
    # we need those in floatX for interpolation and in int64 for indexing.
    x0_f = theano.tensor.floor(x)
    y0_f = theano.tensor.floor(y)
    x1_f = x0_f + 1
    y1_f = y0_f + 1

    x0, y0, x1, y1 = (None, None, None, None)
    # for indexing, we need to take care of the border mode for outside pixels.
    if border_mode == 'nearest':
        x0 = theano.tensor.clip(x0_f, 0, width_f - 1)
        x1 = theano.tensor.clip(x1_f, 0, width_f - 1)
        y0 = theano.tensor.clip(y0_f, 0, height_f - 1)
        y1 = theano.tensor.clip(y1_f, 0, height_f - 1)
    elif border_mode == 'mirror':
        w = 2 * (width_f - 1)
        x0 = theano.tensor.minimum(x0_f % w, -x0_f % w)
        x1 = theano.tensor.minimum(x1_f % w, -x1_f % w)
        h = 2 * (height_f - 1)
        y0 = theano.tensor.minimum(y0_f % h, -y0_f % h)
        y1 = theano.tensor.minimum(y1_f % h, -y1_f % h)
    elif border_mode == 'wrap':
        x0 = theano.tensor.mod(x0_f, width_f)
        x1 = theano.tensor.mod(x1_f, width_f)
        y0 = theano.tensor.mod(y0_f, height_f)
        y1 = theano.tensor.mod(y1_f, height_f)
    else:
        raise ValueError("border_mode must be one of "
                         "'nearest', 'mirror', 'wrap'")
    x0, x1, y0, y1 = (theano.tensor.cast(v, 'int64') for v in (x0, x1, y0, y1))

    # The input is [num_batch, height, width, channels]. We do the lookup in
    # the flattened input, i.e [num_batch*height*width, channels]. We need
    # to offset all indices to match the flat version
    dim2 = width
    dim1 = width * height
    base = theano.tensor.repeat(
        theano.tensor.arange(num_batch, dtype='int64') * dim1, out_height * out_width)
    base_y0 = base + y0 * dim2
    base_y1 = base + y1 * dim2
    idx_a = base_y0 + x0
    idx_b = base_y1 + x0
    idx_c = base_y0 + x1
    idx_d = base_y1 + x1

    # use indices to lookup pixels for all samples
    inp_flat = inp_transposed.reshape((-1, num_channels))
    Ia = inp_flat[idx_a]
    Ib = inp_flat[idx_b]
    Ic = inp_flat[idx_c]
    Id = inp_flat[idx_d]

    # calculate interpolated values
    wa = ((x1_f - x) * (y1_f - y)).dimshuffle(0, 'x')
    wb = ((x1_f - x) * (y - y0_f)).dimshuffle(0, 'x')
    wc = ((x - x0_f) * (y1_f - y)).dimshuffle(0, 'x')
    wd = ((x - x0_f) * (y - y0_f)).dimshuffle(0, 'x')
    transformed_inputs_flat = theano.tensor.sum([wa * Ia, wb * Ib, wc * Ic, wd * Id], axis=0)
    transformed_inputs = theano.tensor.reshape(transformed_inputs_flat,
                                               (num_batch, out_height, out_width, num_channels),
                                               ndim=4)
    # dimshuffle tensor from NHWC to NCHW format
    output = transformed_inputs.dimshuffle(0, 3, 1, 2)
    return output


def transformer_gradi_impl(inp, grid, grad_out, border_mode):
    out = transformer_sampler_impl(inp, grid, border_mode)
    grad_inp = theano.tensor.grad(None, inp, known_grads={out: grad_out})
    grad_grid = theano.tensor.grad(None, grid, known_grads={out: grad_out})
    return (grad_inp, grad_grid)


def transformer_gradt_impl(theta, grad_grid):
    num_batch = theta.shape[0]
    out_height, out_width = grad_grid.shape[2:]
    out_dims = (num_batch, 1, out_height, out_width)
    grid_out = transformer_grid_impl(theta, out_dims)
    return theano.tensor.grad(None, theta, known_grads={grid_out: grad_grid})
