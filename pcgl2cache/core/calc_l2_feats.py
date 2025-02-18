import fastremap
from edt import edt
from collections import Counter
from collections import defaultdict

import numpy as np
from sklearn import decomposition

import warnings

warnings.filterwarnings("ignore", category=RuntimeWarning)

from kvdbclient import BigTableClient


def get_l2_seg(cg, cv, chunk_coord, chunk_size, timestamp, l2_ids=None):
    bbox = np.array(cv.bounds.to_list())

    vol_coord_start = bbox[:3] + chunk_coord
    vol_coord_end = vol_coord_start + chunk_size

    vol = cv[
        vol_coord_start[0] : vol_coord_end[0],
        vol_coord_start[1] : vol_coord_end[1],
        vol_coord_start[2] : vol_coord_end[2],
    ][..., 0]

    sv_ids = fastremap.unique(vol)
    sv_ids = sv_ids[sv_ids != 0]

    if len(sv_ids) == 0:
        return vol.astype(np.uint32), {}

    _l2_ids = cg.get_roots(sv_ids, stop_layer=2, time_stamp=timestamp)
    if l2_ids is not None:
        mapping = {}
        l2id_children_d = cg.get_children(l2_ids)
        for _id in l2_ids:
            children = l2id_children_d[_id]
            try:
                idx = np.where(sv_ids == children[0])[0][0]
            except IndexError:
                continue
            parent = _l2_ids[idx]
            mapping[parent] = _id
        fastremap.remap(_l2_ids, mapping, in_place=True, preserve_missing_labels=True)

    u_l2_ids = fastremap.unique(_l2_ids)
    u_cont_ids = np.arange(1, 1 + len(u_l2_ids))
    cont_ids = fastremap.remap(_l2_ids, dict(zip(u_l2_ids, u_cont_ids)))
    fastremap.remap(
        vol, dict(zip(sv_ids, cont_ids)), preserve_missing_labels=True, in_place=True
    )
    return vol.astype(np.uint32), dict(zip(u_cont_ids, u_l2_ids))


def dist_weight(cv, coords):
    mean_coord = np.mean(coords, axis=0)
    dists = np.linalg.norm((coords - mean_coord) * cv.resolution, axis=1)
    return 1 - dists / dists.max()


def calculate_features(cv, chunk_coord, vol_l2, l2_dict, l2_ids=None):
    from . import attributes

    # First calculate eucledian distance transform for all segments
    # Every entrie in vol_dt is the distance in nm from the closest
    # boundary
    vol_dt = edt(
        vol_l2,
        anisotropy=cv.resolution,
        black_border=False,
        parallel=1,  # number of threads, <= 0 sets to num cpu
    )

    # To efficiently map measured distances from the EDT to all IDs
    # we use `fastremap.inverse_component_map`. This function takes
    # two equally sized volumes - the first has the IDs, the second
    # the data we want to map. However, this function uniquenifies
    # the data entries per ID such that we loose the size information.
    # Additionally, we want to retain information about the locations.
    # To enable this with one iteration of the block, we build a
    # compound data block. Each value has 64 bits, the first 32 bits
    # encode the EDT, the second the location as flat index. Using,
    # float data for the edt would lead to overflows, so we first
    # convert to uints.
    shape = np.array(vol_l2.shape)
    size = np.product(shape)
    stack = ((vol_dt.astype(np.uint64).flatten()) << 32) + np.arange(
        size, dtype=np.uint64
    )

    # cmap_stack is a dictionary of (L2) IDs -> list of 64 bit values
    # encoded as described above.
    cmap_stack = fastremap.inverse_component_map(vol_l2.flatten(), stack)
    if l2_ids is None:
        l2_ids = np.array(list(cmap_stack.keys()))
        l2_ids = l2_ids[l2_ids != 0]
    else:
        l2_dict_reverse = {v: k for k, v in l2_dict.items()}
        _l2_ids = []
        for k in l2_ids:
            try:
                _l2_ids.append(l2_dict_reverse[k])
            except KeyError:
                print(f"Unable to process L2 ID {k}")
                continue
        l2_ids = np.array(_l2_ids)
        if l2_ids.size == 0:
            return {}

    # Initiliaze PCA
    pca = decomposition.PCA(3)

    l2_max_coords = []
    l2_max_scaled_coords = []
    l2_bboxs = []
    l2_chunk_intersects = []
    l2_max_dts = []
    l2_mean_dts = []
    l2_sizes = []
    l2_pca_comps = []
    l2_pca_vals = []
    for l2_id in l2_ids:
        # We first disentangle the compound data for the specific L2 ID
        # and transform the flat indices to 3d indices.
        l2_stack = np.array(cmap_stack[l2_id], dtype=np.uint64)
        dts = l2_stack >> 32
        idxs = l2_stack.astype(np.uint32)
        coords = np.array(np.unravel_index(np.array(idxs), vol_l2.shape)).T

        # Finally, we compute statistics from the decoded data.
        max_idx = np.argmax(dts)
        l2_max_coords.append(coords[max_idx])
        l2_max_scaled_coords.append(coords[np.argmax(dts * dist_weight(cv, coords))])
        l2_bboxs.append([np.min(coords, axis=0), np.max(coords, axis=0)])
        l2_sizes.append(len(idxs))
        l2_max_dts.append(dts[max_idx])
        l2_mean_dts.append(np.mean(dts))
        l2_chunk_intersects.append(
            [np.sum(coords == 0, axis=0), np.sum((coords - vol_l2.shape) == 0, axis=0)]
        )

        # The PCA calculation is straight-forward as long as the are sufficiently
        # many coordinates. We observed long runtimes for very large components.
        # Using a subset of the points in such cases proved to produce almost
        # identical results.
        if len(coords) < 3:
            coords_p = np.concatenate([coords, coords, coords])
        elif len(coords) > 10000:
            coords_p = np.array(
                np.unravel_index(
                    np.random.choice(idxs, 10000, replace=False), vol_l2.shape
                )
            ).T
        else:
            coords_p = coords

        pca.fit(coords_p * cv.resolution)
        l2_pca_comps.append(pca.components_)
        l2_pca_vals.append(pca.singular_values_)

    # In a last step we adjust for the chunk offset.
    offset = chunk_coord + np.array(cv.bounds.to_list()[:3])
    l2_sizes = np.array(np.array(l2_sizes) * np.product(cv.resolution))
    l2_max_dts = np.array(l2_max_dts)
    l2_mean_dts = np.array(l2_mean_dts)
    l2_max_coords = np.array((np.array(l2_max_coords) + offset) * cv.resolution)
    l2_max_scaled_coords = np.array(
        (np.array(l2_max_scaled_coords) + offset) * cv.resolution
    )
    l2_bboxs = np.array(l2_bboxs) + offset
    l2_pca_comps = np.array(l2_pca_comps)
    l2_pca_vals = np.array(l2_pca_vals)
    l2_chunk_intersects = np.array(l2_chunk_intersects)

    # Area calculations are handled seaprately and are performed by overlap through
    # shifts. We shift in each dimension and calculate the overlapping segment ids.
    # The overlapping IDs are then counted per dimension and added up after
    # adjusting for resolution. This measurement will overestimate area slightly
    # but smoothed measurements are ill-defined and too compute intensive.
    x_m = vol_l2[1:] != vol_l2[:-1]
    y_m = vol_l2[:, 1:] != vol_l2[:, :-1]
    z_m = vol_l2[:, :, 1:] != vol_l2[:, :, :-1]

    u_x, c_x = fastremap.unique(
        np.concatenate([vol_l2[1:][x_m], vol_l2[:-1][x_m]]), return_counts=True
    )
    u_y, c_y = fastremap.unique(
        np.concatenate([vol_l2[:, 1:][y_m], vol_l2[:, :-1][y_m]]), return_counts=True
    )
    u_z, c_z = fastremap.unique(
        np.concatenate([vol_l2[:, :, 1:][z_m], vol_l2[:, :, :-1][z_m]]),
        return_counts=True,
    )

    x_area = np.product(cv.resolution[[1, 2]])
    y_area = np.product(cv.resolution[[0, 2]])
    z_area = np.product(cv.resolution[[0, 1]])

    x_dict = Counter(dict(zip(u_x, c_x * x_area)))
    y_dict = Counter(dict(zip(u_y, c_y * y_area)))
    z_dict = Counter(dict(zip(u_z, c_z * z_area)))

    area_dict = x_dict + y_dict + z_dict
    areas = np.array([area_dict[l2_id] for l2_id in l2_ids])

    return {
        "l2id": fastremap.remap(l2_ids, l2_dict).astype(attributes.UINT64.type),
        "size_nm3": l2_sizes.astype(attributes.UINT32.type),
        "area_nm2": areas.astype(attributes.UINT32.type),
        "max_dt_nm": l2_max_dts.astype(attributes.UINT16.type),
        "mean_dt_nm": l2_mean_dts.astype(attributes.FLOAT16.type),
        "rep_coord_nm": l2_max_scaled_coords.astype(attributes.UINT64.type),
        "chunk_intersect_count": l2_chunk_intersects.astype(attributes.UINT16.type),
        "pca_comp": l2_pca_comps.astype(attributes.FLOAT16.type),
        "pca_vals": l2_pca_vals.astype(attributes.FLOAT32.type),
    }


def download_and_calculate(cg, cv, chunk_coord, chunk_size, timestamp, l2_ids):
    vol_l2, l2_dict = get_l2_seg(
        cg, cv, chunk_coord, chunk_size, timestamp, l2_ids=l2_ids
    )
    if np.sum(np.array(list(l2_dict.values())) != 0) == 0:
        return {}
    return calculate_features(cv, chunk_coord, vol_l2, l2_dict, l2_ids)


def _l2cache_thread(cg, cv, chunk_coord, timestamp, l2_ids):
    chunk_size = cg.chunk_size.astype(np.int)
    chunk_coord = chunk_coord * chunk_size
    return download_and_calculate(cg, cv, chunk_coord, chunk_size, timestamp, l2_ids)


def run_l2cache(cg, cv_path, chunk_coord=None, timestamp=None, l2_ids=None):
    from datetime import datetime
    from cloudvolume import CloudVolume

    if chunk_coord is None:
        assert l2_ids is not None and len(l2_ids) > 0
        chunk_coord = cg.get_chunk_coordinates(l2_ids[0])
    chunk_coord = np.array(list(chunk_coord), dtype=int)
    cv = CloudVolume(
        cv_path, bounded=False, fill_missing=True, progress=False, mip=cg.cv.mip
    )
    return _l2cache_thread(cg, cv, chunk_coord, timestamp, l2_ids)


def run_l2cache_batch(cg, cv_path, chunk_coords, timestamp=None):
    ret_dicts = []
    for chunk_coord in chunk_coords:
        ret_dicts.append(run_l2cache(cg, cv_path, chunk_coord, timestamp))

    comb_ret_dict = defaultdict(list)
    for ret_dict in ret_dicts:
        for k in ret_dict:
            comb_ret_dict[k].extend(ret_dict[k])
    return comb_ret_dict


def write_to_db(client: BigTableClient, result_d: dict) -> None:
    from . import attributes
    from kvdbclient.base import Entry
    from kvdbclient.base import EntryKey

    entries = []
    for tup in zip(*result_d.values()):
        (
            l2id,
            size_nm3,
            area_nm2,
            max_dt_nm,
            mean_dt_nm,
            rep_coord_nm,
            chunk_intersect_count,
            pca_comp,
            pca_vals,
        ) = tup
        val_d = {
            attributes.SIZE_NM3: size_nm3,
            attributes.AREA_NM2: area_nm2,
            attributes.MAX_DT_NM: max_dt_nm,
            attributes.MEAN_DT_NM: mean_dt_nm,
            attributes.REP_COORD_NM: rep_coord_nm,
            attributes.CHUNK_INTERSECT_COUNT: chunk_intersect_count,
            attributes.PCA: pca_comp,
            attributes.PCA_VAL: pca_vals,
        }
        entries.append(Entry(EntryKey(l2id), val_d))
    client.write_entries(entries)
