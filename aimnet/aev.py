import math
from typing import List, Optional, Dict, Tuple

import torch
from torch import nn, Tensor

from aimnet import ops, nbops


class AEVSV(nn.Module):
    """AEV module to expand distances and vectors toneighbors over shifted Gaussian basis functions.

    Parameters:
    -----------
    rmin : float, optional
        Minimum distance for the Gaussian basis functions. Default is 0.8.
    rc_s : float, optional
        Cutoff radius for scalar features. Default is 5.0.
    nshifts_s : int, optional
        Number of shifts for scalar features. Default is 16.
    eta_s : Optional[float], optional
        Width of the Gaussian basis functions for scalar features. Will estimate reasonable default.
    rc_v : Optional[float], optional
        Cutoff radius for vector features. Default is same as `rc_s`.
    nshifts_v : Optional[int], optional
        Number of shifts for vector features. Default is same as `nshifts_s`
    eta_v : Optional[float], optional
        Width of the Gaussian basis functions for vector features. Will estimate reasonable default.
    shifts_s : Optional[List[float]], optional
        List of shifts for scalar features. Default equidistant between `rmin` and `rc_s`
    shifts_v : Optional[List[float]], optional
        List of shifts for vector features. Default equidistant between `rmin` and `rc_v`
    """

    def __init__(
        self,
        rmin: float = 0.8,
        rc_s: float = 5.0,
        nshifts_s: int = 16,
        eta_s: Optional[float] = None,
        rc_v: Optional[float] = None,
        nshifts_v: Optional[int] = None,
        eta_v: Optional[float] = None,
        shifts_s: Optional[List[float]] = None,
        shifts_v: Optional[List[float]] = None,
    ):
        super().__init__()

        self._init_basis(rc_s, eta_s, nshifts_s, shifts_s, rmin, mod="_s")
        if rc_v is not None:
            assert rc_v <= rc_s
            assert nshifts_v is not None
            self._init_basis(rc_v, eta_v, nshifts_v, shifts_v, rmin, mod="_v")
            self._dual_basis = True
        else:
            # dummy init
            self._init_basis(rc_s, eta_s, nshifts_s, shifts_s, rmin, mod="_v")
            self._dual_basis = False

        self.dmat_fill = rc_s

    def _init_basis(self, rc, eta, nshifts, shifts, rmin, mod="_s"):
        self.register_parameter(
            "rc" + mod,
            nn.Parameter(torch.tensor(rc, dtype=torch.float), requires_grad=False),
        )
        if eta is None:
            eta = (1 / ((rc - rmin) / nshifts)) ** 2
        self.register_parameter(
            "eta" + mod,
            nn.Parameter(torch.tensor(eta, dtype=torch.float), requires_grad=False),
        )
        if shifts is None:
            shifts = torch.linspace(rmin, rc, nshifts + 1)[:nshifts]
        else:
            shifts = torch.as_tensor(shifts, dtype=torch.float)
        self.register_parameter(
            "shifts" + mod, nn.Parameter(shifts, requires_grad=False)
        )

    def forward(self, data: Dict[str, Tensor]) -> Dict[str, Tensor]:
        # shapes (..., m) and (..., m, 3)
        d_ij, r_ij = ops.calc_distances(data)
        data["d_ij"] = d_ij
        # shapes (..., nshifts, m) and (..., nshifts, 3, m)
        u_ij, gs, gv = self._calc_aev(r_ij, d_ij, data)
        # for now, do not save u_ij
        data["gs"], data["gv"] = gs, gv
        return data

    def _calc_aev(self, r_ij: Tensor, d_ij: Tensor, data: Dict[str, Tensor]) -> Tuple[Tensor, Tensor, Tensor]:
        # n and m can be different because of the neighbor mask.
        fc_ij = ops.cosine_cutoff(d_ij, self.rc_s)  # (..., m)
        fc_ij = nbops.mask_ij_(fc_ij, data, 0.0)
        gs = ops.exp_expand(d_ij, self.shifts_s, self.eta_s) * \
            fc_ij.unsqueeze(-2)  # (b, n, nshifts, m) * (b, n, 1, m) -> (b, n, nshifts, m)
        u_ij = r_ij.mT.contiguous() / \
            d_ij.unsqueeze(-2)  # (b, n, 3, m) / (b, n, 1, m) -> (b, n, 3, m)
        if self._dual_basis:
            fc_ij = ops.cosine_cutoff(d_ij, self.rc_v)
            gsv = ops.exp_expand(d_ij, self.shifts_v, self.eta_v) * fc_ij.unsqueeze(-2)
            gv = gsv.unsqueeze(-2) * u_ij.unsqueeze(-3)
        else:
            # (b, n, nshifts, 1, m), (b, n, 1, m, 3) -> (b, n, nshifts, 3, m)
            gv = gs.unsqueeze(-2) * u_ij.unsqueeze(-3)
        return u_ij, gs, gv # gv is gu in the paper


class ConvSV(nn.Module):
    """AIMNet2 type convolution: encoding of local environment which combines geometry of local environment and atomic features.

    Parameters:
    -----------
    nshifts_s : int
        Number of shifts (gaussian basis functions) for scalar convolution.
    nchannel : int
        Number of feature channels for atomic features.
    d2features : bool, optional
        Flag indicating whether to use 2D features. Default is False.
    do_vector : bool, optional
        Flag indicating whether to perform vector convolution. Default is True.
    nshifts_v : Optional[int], optional
        Number of shifts for vector convolution. If not provided, defaults to the value of nshifts_s.
    ncomb_v : Optional[int], optional
        Number of linear combinations for vector features. If not provided, defaults to the value of nshifts_v.
    """

    def __init__(
        self,
        nshifts_s: int,
        nchannel: int,
        d2features: bool = False,
        do_vector: bool = True,
        nshifts_v: Optional[int] = None,
        ncomb_v: Optional[int] = None,
    ):
        super().__init__()
        nshifts_v = nshifts_v or nshifts_s
        ncomb_v = ncomb_v or nshifts_v
        agh = _init_ahg(nchannel, nshifts_v, ncomb_v)
        # (a, g, h) -> (h, g, a)
        # agh = agh.permute(2, 1, 0).contiguous()
        self.register_parameter("agh", nn.Parameter(agh, requires_grad=True))
        self.do_vector = do_vector
        self.nchannel = nchannel
        self.d2features = d2features
        self.nshifts_s = nshifts_s
        self.nshifts_v = nshifts_v
        self.ncomb_v = ncomb_v

    def output_size(self):
        n = self.nchannel * self.nshifts_s
        if self.do_vector:
            n += self.nchannel * self.ncomb_v
        return n

    def forward(self, a: Tensor, gs: Tensor, gv: Optional[Tensor] = None) -> Tensor:
        avf = []
        if self.d2features:
            avf_s = torch.einsum("...gam,...gm->...ag", a, gs)
        else:
            avf_s = torch.einsum("...gm,...ma->...ag", gs, a)
        avf.append(avf_s.flatten(-2, -1))
        if self.do_vector:
            assert gv is not None
            agh = self.agh
            if self.d2features:
                avf_v = torch.einsum("...gam,...gdm,agh->...ahd", a, gv, agh)
            else:
                avf_v = torch.einsum("...ma,...gdm,agh->...ahd", a, gv, agh)
            avf.append(avf_v.pow(2).sum(-1).flatten(-2, -1)) # Again, why pow(2)?
        return torch.cat(avf, dim=-1)


def _init_ahg(b, m, n):
    ret = torch.zeros(b, m, n)
    for i in range(b):
        ret[i] = _init_ahg_one(m, n).t()
    return ret


def _init_ahg_one(n, m):
    # make x8 times more vectors to select most diverse
    x = torch.arange(n).unsqueeze(0)
    a1, a2, a3, a4 = torch.randn(8 * m, 4).unsqueeze(-2).unbind(-1)
    y = a1 * torch.sin(a2 * 2 * x * math.pi / n) + a3 * torch.cos(
        a4 * 2 * x * math.pi / n
    )
    y -= y.mean(dim=-1, keepdim=True)
    y /= y.std(dim=-1, keepdim=True)

    dmat = torch.cdist(y, y)
    # most distant point
    ret = torch.zeros(m, n)
    mask = torch.ones(y.shape[0], dtype=torch.bool)
    i = dmat.sum(-1).argmax()
    ret[0] = y[i]
    mask[i] = False

    # simple maxmin impementation
    for j in range(1, m):
        mindist, _ = torch.cdist(ret[:j], y).min(dim=0)
        maxidx = torch.argsort(mindist)[mask][-1]
        assert mask[maxidx] == True
        ret[j] = y[maxidx]
        mask[maxidx] = False
    return ret
