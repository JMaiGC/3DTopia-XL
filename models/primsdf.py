import torch
import trimesh
import torch.nn as nn
import torch.nn.functional as F
from simple_knn._C import distCUDA2
from pytorch3d.ops import sample_farthest_points

import logging

logger = logging.getLogger(__name__)

class PrimSDF(nn.Module):
    def __init__(self, mesh_obj=None, f_sdf=None, geo_fn=None, asset_list=None, num_prims=1024, dim_feat=6, prim_shape=8, init_scale=0.05, sdf2alpha_var=0.005, auto_scale_init=True, init_sampling="uniform"):
        super().__init__()
        self.num_prims = num_prims
        # 6 channels features - [SDF, R, G, B, roughness, metallic]
        self.dim_feat = dim_feat
        self.prim_shape = prim_shape
        self.sdf_sampled_point = None
        self.auto_scale_init = auto_scale_init
        self.init_sampling = init_sampling
        self.sdf2alpha_var = sdf2alpha_var

        # assume the mesh is normalized to [-1, 1] cube
        self.mesh_obj = mesh_obj
        self.f_sdf = f_sdf
        # N x (D x S^3 + 3(Global Translation) + 1(Global Scale))
        self.srt_param = nn.parameter.Parameter(torch.zeros(self.num_prims, 1 + 3))
        self.feat_param = nn.parameter.Parameter(torch.zeros(self.num_prims, self.dim_feat * (self.prim_shape ** 3)))
        self.geo_start_index = 0
        self.geo_end_index = self.geo_start_index + self.prim_shape ** 3 # non-inclusive
        self.tex_start_index = self.geo_end_index
        self.tex_end_index = self.tex_start_index + self.prim_shape ** 3 * 3 # non-inclusive
        self.mat_start_index = self.tex_end_index
        self.mat_end_index = self.mat_start_index + self.prim_shape ** 3 * 2

        # sampled_point -> local grid
        # local_grid - [prim_shape^3, 3]
        xx = torch.linspace(-1, 1, self.prim_shape)
        # two ways to sample xyz-axis aligned local grids: 1st is ij indexing
        meshx, meshy, meshz = torch.meshgrid(xx, xx, xx, indexing='ij')
        local_grid = torch.stack((meshz, meshy, meshx), dim=-1).reshape(-1, 3)
        self.local_grid = local_grid
        # second is xy indexing, equivalent to the first one
        # meshx, meshy, meshz = torch.meshgrid(xx, xx, xx, indexing='xy')
        # local_grid = torch.stack((meshz, meshx, meshy), dim=-1).reshape(-1, 3)
        if self.f_sdf is not None and geo_fn is not None and asset_list is not None:
            self._init_param(init_scale=init_scale, geo_fn=geo_fn, asset_list=asset_list, sampling=self.init_sampling)

    @torch.no_grad()
    def _init_param(self, init_scale, geo_fn, asset_list, sampling="uniform"):
        if sampling == 'uniform':
            sampled_point, _ = trimesh.sample.sample_surface(self.mesh_obj, 500000)
            sampled_point = torch.from_numpy(sampled_point).float()
            sampled_point, _ = sample_farthest_points(sampled_point[None, ...], K=self.num_prims)
            sampled_point = sampled_point[0]
            self.srt_param[:, 1:4] = sampled_point
            # init scale
            if self.auto_scale_init:
                logger.info(f'[PrimSDF] uses auto scale initialization...')
                dist2 = torch.clamp_min(distCUDA2(self.pos.cuda()), 1e-6).cpu()
                # TODO: not sure use one global min dist for all or different dist
                # self.srt_param[:, 0:1] = torch.ones_like(self.srt_param[:, 0:1]) * torch.min(torch.sqrt(dist2))
                self.srt_param[:, 0:1] = torch.sqrt(dist2)[:, None]
            else:
                logger.info(f'[PrimSDF] uses predefined initial scale {init_scale}')
                self.srt_param[:, 0:1] = torch.ones_like(self.srt_param[:, 0:1]) * init_scale
        elif sampling == 'farthest':
            sampled_point, sampled_ind = sample_farthest_points(self.mesh_obj.v[None, ...], K=self.num_prims - 512)
            sampled_point = sampled_point[0]
            self.srt_param[:, 1:4] = sampled_point
            # init scale
            if self.auto_scale_init:
                logger.info(f'[PrimSDF] uses auto scale initialization...')
                dist2 = torch.clamp_min(distCUDA2(self.pos.cuda()), 1e-6).cpu()
                # TODO: not sure use one global min dist for all or different dist
                # self.srt_param[:, 0:1] = torch.ones_like(self.srt_param[:, 0:1]) * torch.min(torch.sqrt(dist2))
                self.srt_param[:, 0:1] = torch.sqrt(dist2)[:, None]
            else:
                logger.info(f'[PrimSDF] uses predefined initial scale {init_scale}')
                self.srt_param[:, 0:1] = torch.ones_like(self.srt_param[:, 0:1]) * init_scale
        elif sampling == 'coverage':
            num_coverage_pts_check = 512
            sampled_point, sampled_ind = sample_farthest_points(self.mesh_obj.v[None, ...], K=self.num_prims - num_coverage_pts_check)
            coverage_check_pts = self.mesh_obj.sample_surface(50000)
            sampled_point = sampled_point[0]
            dist2 = torch.clamp_min(distCUDA2(sampled_point.cuda()), 1e-6).cpu()
            sampled_scale = torch.sqrt(dist2)[:, None]
            # coverage check
            normalized_pts = (coverage_check_pts[:, None, :] - sampled_point[None, :, :]) / sampled_scale[None, ...]
            pts_norm = torch.norm(normalized_pts, p=float('inf'), dim=-1)
            min_norm, _ = torch.min(pts_norm, dim=1)
            not_covered_mask = min_norm > 1
            not_covered_pts = coverage_check_pts[not_covered_mask, :]
            recovered_pts, _ = sample_farthest_points(not_covered_pts[None, ...], K=num_coverage_pts_check)
            sampled_point = torch.concat([sampled_point, recovered_pts[0]], dim=0)
            recovered_pts_scale = torch.ones(num_coverage_pts_check, 1) * torch.mean(sampled_scale)
            sampled_scale = torch.concat([sampled_scale, recovered_pts_scale], dim=0)
            # init pos
            self.srt_param[:, 1:4] = sampled_point
            self.srt_param[:, 0:1] = sampled_scale
        else:
            raise NotImplementedError("[{}] sampling is not support!".format(sampling))

        local_grid = self.local_grid[None, ...] # [1, ps^3, 3]
        prim_scale = self.scale[..., None] # [N, 1, 1]
        prim_pos = self.pos[:, None, :] # [N, 1, 3]
        sdf_sampled_point = prim_pos + prim_scale * local_grid
        self.sdf_sampled_point = sdf_sampled_point
        sdf_sampled_point = sdf_sampled_point.reshape(self.num_prims * self.prim_shape ** 3, 3)
        raw_sdf = self.f_sdf.signed_distance(sdf_sampled_point, return_uvw=False, mode='raystab')[0].cpu().numpy() * (-1)

        # Given a list of geo_fn, we first find the nearest geometry part, then do texture sampling
        dist_list = []
        for idx, gfn in enumerate(geo_fn):
            # self.pos - [nprims, 3]
            # gfn.v - [N, 3]
            # dist - [nprims, N]
            dist = torch.norm((self.pos[:, None, :] - gfn.v[None, :, :]), p=2, dim=-1)
            min_dist, _ = dist.min(-1)
            dist_list.append(min_dist)
        total_dist = torch.stack(dist_list, dim=-1)
        _, prim_geo_idx = total_dist.min(-1)

        init_tex = torch.zeros(self.num_prims, self.prim_shape**3, 3)
        init_mat = torch.zeros(self.num_prims, self.prim_shape**3, 2)
        for idx, gfn in enumerate(geo_fn):
            prim_mask = (prim_geo_idx == idx)
            num_pts = prim_mask.sum().item()
            if num_pts == 0:
            # there is a chance that nothing is sampled according to prim_mask, especially for geo_fn with a small number of vertices
                continue
            masked_sampled_points = self.sdf_sampled_point[prim_mask, :, :].reshape(-1, 3)
            masked_sampled_rawtex, _ = gfn.sample_uv_from_3dpts(masked_sampled_points.numpy(), asset_list[idx].albedo)
            masked_sampled_rawmat, _ = gfn.sample_uv_from_3dpts(masked_sampled_points.numpy(), asset_list[idx].metallicRoughness)
            init_tex[prim_mask, :, :] = torch.from_numpy(masked_sampled_rawtex).reshape(num_pts, self.prim_shape ** 3, 3)
            init_mat[prim_mask, :, :] = torch.from_numpy(masked_sampled_rawmat).reshape(num_pts, self.prim_shape ** 3, 3)[..., -2:]

        # init sdf
        self.feat_param[:, self.geo_start_index:self.geo_end_index] = torch.from_numpy(raw_sdf).reshape(self.num_prims, self.prim_shape ** 3)
        # init albedo
        self.feat_param[:, self.tex_start_index:self.tex_end_index] = init_tex.permute(0, 2, 1).reshape(self.num_prims, 3 * self.prim_shape ** 3)
        # init metallicRoughness
        self.feat_param[:, self.mat_start_index:self.mat_end_index] = init_mat.permute(0, 2, 1).reshape(self.num_prims, 2 * self.prim_shape ** 3)

        # safe guard
        nan_mask = torch.isnan(self.feat_param)
        self.feat_param[nan_mask] = 0

    def forward(self, x):
        # x - [bs, 3]
        bs = x.shape[0]
        weights = self.prim_weight(x)
        output = self.grid_sample_feat(x, weights)
        preds = {}
        preds['sdf'] = output[:, 0:1]
        # RGB
        preds['tex'] = torch.clip(output[:, 1:4], min=0.0, max=1.0)
        # roughness, metallic
        preds['mat'] = torch.clip(output[:, 4:6], min=0.0, max=1.0)
        return preds
        
    def grid_sample_feat(self, x, weights):
        # implementation of I_V -> trilinear grid sample of V_i
        # x - [bs, 3]
        # weights - [bs, n_prims]
        bs = x.shape[0]
        sampled_point = (x[:, None, :] - self.pos[None, ...]) / self.scale[None, ...]
        mask = weights > 0
        ind_bs, ind_nprim = torch.where(weights > 0)
        masked_sampled_point = sampled_point[ind_bs, ind_nprim, :].reshape(ind_nprim.shape[0], 1, 1, 1, 3)
        feat4sample = self.feat[ind_nprim, :].reshape(ind_nprim.shape[0], self.dim_feat, self.prim_shape, self.prim_shape, self.prim_shape)
        
        sampled_feat = F.grid_sample(feat4sample, masked_sampled_point, mode='bilinear', padding_mode='zeros', align_corners=True).reshape(ind_nprim.shape[0], self.dim_feat)
        weighted_sampled_feat = sampled_feat * weights[mask][:, None]
        weighted_feat = torch.zeros(bs, self.dim_feat).to(x)
        weighted_feat.index_add_(0, ind_bs, weighted_sampled_feat)

        # at inference time, fill in approximated SDF value for region not covered by prims
        if not self.training:
            # get mask for points not covered by prims
            bs_mask = weights.sum(1) <= 0

            # get nearest prim index
            dist = torch.norm(x[bs_mask, None, :] - self.pos[None, ...], p=2, dim=-1)
            _, min_dist_ind = dist.min(1)
            nearest_prim_pos = self.pos[min_dist_ind, :]
            nearest_prim_scale = self.scale[min_dist_ind, :]

            # in each nearest prim, get nearest voxel points
            candidate_nearest_pts = nearest_prim_pos[:, None, :] + nearest_prim_scale[..., None] * self.local_grid.to(x)[None, :]
            pts_dist = torch.norm(x[bs_mask, None, :] - candidate_nearest_pts, p=2, dim=-1)
            min_dist, min_dist_pts_ind = pts_dist.min(1)

            # get the SDF value as a nearest valid SDF value
            min_pts_sdf = self.feat_geo[min_dist_ind, min_dist_pts_ind]
            # approximate SDF value with the same sign distance + L2 distance
            approx_sdf = min_pts_sdf + min_dist * torch.sign(min_pts_sdf)
            weighted_feat[bs_mask, 0:1] = approx_sdf[:, None]            
        return weighted_feat
    
    def prim_weight(self, x):
        # x - [bs, 3]
        weights = F.relu(1 - torch.norm((x[:, None, :] - self.pos[None, ...]) / self.scale[None, ...], p = float('inf'), dim=-1))
        # weight - [bs, N]
        normalized_weights = weights / (torch.sum(weights, dim=-1, keepdim=True) + 1e-6)
        return normalized_weights

    def sdf2alpha(self, sdf):
        return torch.exp(-(sdf / self.sdf2alpha_var) ** 2)

    @property
    def pos(self):
        return self.srt_param[:, 1:4]
    
    @property
    def scale(self):
        return self.srt_param[:, 0:1]
    
    @property
    def feat(self):
        return self.feat_param

    @property
    def feat_geo(self):
        return self.feat_param[:, self.geo_start_index:self.geo_end_index]
    
    @property
    def feat_tex(self):
        return self.feat_param[:, self.tex_start_index:self.tex_end_index]

    @property
    def feat_mat(self):
        return self.feat_param[:, self.mat_start_index:self.mat_end_index]