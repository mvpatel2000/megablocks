import torch
import triton
import triton.language as tl


def assert_is_matrix(x):
    if x.ndim != 2:
        raise ValueError(f"Expected 2-tensor but got {x.ndim}-tensor")


def assert_is_vector(x):
    if x.ndim != 1:
        raise ValueError(f"Expected 1-tensor but got {x.ndim}-tensor")


def assert_equal(a, b):
    if a != b:
        raise ValueError(f"Expected dimensions to be equal but got {a} and {b}.")


@triton.autotune(
    configs=[
        triton.Config({'BLOCK_X': 64}, num_warps=2),
        triton.Config({'BLOCK_X': 128}, num_warps=2),
        triton.Config({'BLOCK_X': 256}, num_warps=2),
        triton.Config({'BLOCK_X': 128}, num_warps=4),
        triton.Config({'BLOCK_X': 256}, num_warps=4),
    ],
    key=['num_columns'],
)
@triton.jit
def _padded_copy(a, b, num_columns, indices, bin_ids, bins, padded_bins,
                 BLOCK_X : tl.constexpr, A_TO_B : tl.constexpr):
    # Our index into array 'a'.
    index_a = tl.load(indices + tl.program_id(0))

    # One threadblock per row in 'a'. Array 'b' has greater or equal
    # number of rows since they could be padded.
    bin_idx = tl.load(bin_ids + tl.program_id(0))

    # Now we know what bin we're assigned to, but we need to know how
    # many threadblocks were assigned to earlier bins so we can offset
    # in our bin properly.
    offset_in_bin = tl.program_id(0);
    if bin_idx > 0:
        offset_in_bin -= tl.load(bins + bin_idx - 1)

    # Load the starting index of our bin in array 'b'.
    index_b = offset_in_bin;
    if bin_idx > 0:
        index_b += tl.load(padded_bins + bin_idx - 1)

    # Offset the input and output pointers.
    a += index_a * num_columns
    b += index_b * num_columns
    offsets = tl.arange(0, BLOCK_X)

    # Swap the pointers depending on the direction.
    iptr = a if A_TO_B else b
    optr = b if A_TO_B else a

    iterations = tl.cdiv(num_columns, BLOCK_X)
    for i in range(tl.cdiv(num_columns, BLOCK_X)):
        mask = offsets < num_columns
        x = tl.load(iptr + offsets, mask=mask)
        tl.store(optr + offsets, x, mask=mask)

        offsets += BLOCK_X

# x: (tokens, hidden_size), real.
# indices: (tokens,), integer.
# bin_ids: (tokens,), integer.
# bins: (num_experts,), integer.
# padded_bins: (num_experts,), integer.
def padded_gather(x, indices, bin_ids, bins, padded_bins):
    # Validate the input shapes.
    assert_is_matrix(x)
    assert_is_vector(indices)
    assert_is_vector(bin_ids)
    assert_is_vector(bins)
    assert_is_vector(padded_bins)
    assert_equal(indices.shape[0], x.shape[0])
    assert_equal(bin_ids.shape[0], x.shape[0])
    assert_equal(bins.size(), padded_bins.size())

    # NOTE: Because of the padding, the output size is dynamic.
    # We load the final padded bin bound to get the output rows.
    output_rows = padded_bins[-1].cpu().item()
    out = torch.zeros((output_rows, x.shape[1]), dtype=x.dtype, device=x.device)
    _padded_copy[(x.shape[0],)](
        x, out, x.shape[1], indices, bin_ids, bins, padded_bins, A_TO_B=True)
    return out


# x: (padded_tokens, hidden_size), real.
# indices: (tokens,), integer.
# bin_ids: (tokens,), integer.
# bins: (num_experts,), integer.
# padded_bins: (num_experts,), integer.
def padded_scatter(x, indices, bin_ids, bins, padded_bins):
    # Validate the input shapes.
    assert_is_matrix(x)
    assert_is_vector(indices)
    assert_is_vector(bin_ids)
    assert_is_vector(bins)
    assert_is_vector(padded_bins)
    assert_equal(indices.shape[0], bin_ids.shape[0])
    assert_equal(bins.size(), padded_bins.size())

    out = torch.empty(
        (indices.shape[0], x.shape[1]),
        dtype=x.dtype,
        device=x.device)
    _padded_copy[(out.shape[0],)](
        out, x, x.shape[1], indices, bin_ids, bins, padded_bins, A_TO_B=False)
    return out
