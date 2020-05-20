#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Classes handling the domain definition and associated functionality for LASIF.

Matplotlib is imported lazily to avoid heavy startup costs.

:copyright:
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2014

:license:
    GNU General Public License, Version 3
    (http://www.gnu.org/copyleft/gpl.html)
"""
import pathlib
import typing
import warnings

import numpy as np
from pyexodus import exodus
from scipy.spatial import cKDTree

from lasif import LASIFNotFoundError, LASIFError, LASIFWarning
from lasif.rotations import lat_lon_radius_to_xyz, xyz_to_lat_lon_radius


class ExodusDomain:
    def __init__(self,
                 exodus_file: typing.Union[str, pathlib.Path],
                 num_buffer_elems: int):
        self.exodus_file = str(exodus_file)
        self.num_buffer_elems = num_buffer_elems
        self.r_earth = 6371000
        self.e = None
        self.is_global_mesh = False
        self.domain_edge_tree = None
        self.earth_surface_tree = None
        self.approx_elem_width = None
        self.max_elem_edge_length = None
        self.domain_edge_coords = None
        self.earth_surface_coords = None
        self.KDTrees_initialized = False
        self.min_lat = None
        self.max_lat = None
        self.min_lon = None
        self.max_lon = None
        self.max_depth = None
        self.center_lat = None
        self.center_lon = None
        self.is_read = False
        self.side_set_names = None

    def _read(self):
        """
        Reads the exodus file and gathers basic information such as the
        coordinates of the edge nodes. In the case of domain that spans
         the entire earth, all points will lie inside the domain, therefore
        further processing is not necessary.
        """
        try:
            self.e = exodus(self.exodus_file, mode='r')
        except AssertionError:
            msg = ("Could not open the project's mesh file. "
                   "Please ensure that the path specified "
                   "in config is correct.")
            raise LASIFNotFoundError(msg)

        # if less than 2 side sets, this must be a global mesh.  Return
        self.side_set_names = self.e.get_side_set_names()
        if len(self.side_set_names) <= 2 and 'outer_boundary' \
                not in self.side_set_names:
            self.is_global_mesh = True
            self.min_lat = -90.0
            self.max_lat = 90.0
            self.min_lon = -180.0
            self.max_lon = 180.0
            return
        if 'a0' in self.side_set_names:
            self.is_global_mesh = True
            self.min_lat = -90.0
            self.max_lat = 90.0
            self.min_lon = -180.0
            self.max_lon = 180.0
            return

        side_nodes = []
        earth_surface_nodes = []
        earth_bottom_nodes = []
        for side_set in self.side_set_names:
            if side_set == "surface":
                continue
            if side_set == "r0":
                idx = self.e.get_side_set_ids()[
                    self.side_set_names.index(side_set)]
                _, earth_bottom_nodes = self.e.get_side_set_node_list(idx)
                continue
            idx = self.e.get_side_set_ids()[
                self.side_set_names.index(side_set)]

            if side_set == "r1":
                _, earth_surface_nodes = self.e.get_side_set_node_list(idx)
                continue

            _, nodes_side_set = list(self.e.get_side_set_node_list(idx))
            side_nodes.extend(nodes_side_set)

        # Remove Duplicates
        side_nodes = np.unique(side_nodes)

        # Get node numbers of the nodes specifying the domain boundaries
        boundary_nodes = np.intersect1d(side_nodes, earth_surface_nodes)
        bottom_boundaries = np.intersect1d(side_nodes, earth_bottom_nodes)

        # Deduct 1 from the nodes indices, (exodus is 1 based)
        boundary_nodes -= 1
        bottom_boundaries -= 1
        earth_surface_nodes -= 1
        earth_bottom_nodes -= 1

        # Get coordinates
        points = np.array(self.e.get_coords()).T
        self.domain_edge_coords = points[boundary_nodes]
        self.earth_surface_coords = points[earth_surface_nodes]
        self.earth_bottom_coords = points[earth_bottom_nodes]
        self.bottom_edge_coords = points[bottom_boundaries]

        # Get approximation of element width, take second smallest value
        first_node = self.domain_edge_coords[0, :]
        distances_to_node = self.domain_edge_coords - first_node
        r = np.sqrt(np.sum(distances_to_node ** 2, axis=1))

        self.approx_elem_width = np.sort(r)[2]

        # get max element edge length
        edge_aspect_ratio = self.e.get_element_variable_values(
            1, "edge_aspect_ratio", 1)
        hmin = self.e.get_element_variable_values(1, "hmin", 1)
        self.max_elem_edge_length = np.max(hmin*edge_aspect_ratio)

        # Get extent and center of domain
        x, y, z = self.domain_edge_coords.T

        # get center lat/lon
        x_cen, y_cen, z_cen = np.sum(x), np.sum(y), np.sum(z)
        self.center_lat, self.center_lon, _ = \
            xyz_to_lat_lon_radius(x_cen, y_cen, z_cen)

        # get extent
        lats, lons, r = xyz_to_lat_lon_radius(x, y, z)
        self.min_lat = min(lats)
        self.max_lat = max(lats)
        self.min_lon = min(lons)
        self.max_lon = max(lons)

        # Get coords for the bottom edge of mesh
        x, y, z = self.bottom_edge_coords.T

        # Figure out maximum depth of mesh
        _, _, r = xyz_to_lat_lon_radius(x, y, z)
        min_r = min(r)
        self.max_depth = self.r_earth - min_r

        self.is_read = True

        # Close file
        self.e.close()

    def _initialize_kd_trees(self):
        if not self.is_read:
            self._read()

        # KDTrees not needed in the case of a global mesh
        if self.is_global_mesh:
            return

        # build KDTree that can be used for querying later
        self.earth_surface_tree = cKDTree(self.earth_surface_coords)
        self.domain_edge_tree = cKDTree(self.domain_edge_coords)
        self.KDTrees_initialized = True

    def get_side_set_names(self):
        if not self.is_read:
            self._read()
        return self.side_set_names

    def point_in_domain(self, longitude, latitude, depth=None):
        """
        "Test whether a point lies inside the domain,

        :param longitude: longitude in degrees
        :param latitude: latitude in degrees
        :param depth: depth of event
        """
        if not self.is_read:
            self._read()

        if self.is_global_mesh:
            return True

        if not self.KDTrees_initialized:
            self._initialize_kd_trees()

        # Assuming a spherical Earth without topography
        point_on_surface = lat_lon_radius_to_xyz(
            latitude, longitude, self.r_earth)

        dist, _ = self.earth_surface_tree.query(point_on_surface, k=1)

        # False if not close enough to domain surface, this might go wrong
        # for meshes with significant topography/ellipticity in
        # combination with a small element size.
        if dist > 3 * self.approx_elem_width:
            return False

        # Check whether domain is deep enough to include the point.
        # Multiply element width with 1.5 since they are larger at the bottom
        if depth:
            if depth > (self.max_depth - self.num_buffer_elems *
                        self.approx_elem_width * 1.5):
                return False

        dist, _ = self.domain_edge_tree.query(point_on_surface, k=1)
        # False if too close to edge of domain
        if dist < (self.num_buffer_elems * self.max_elem_edge_length):
            return False

        return True

    def plot(self, ax=None, plot_inner_boundary=True, show_mesh=False):
        """
        Plots the domain
        Global domain is plotted using an equal area Mollweide projection.

        :param ax: matplotlib axes
        :param plot_inner_boundary: plot the convex hull of the mesh
        surface nodes that lie inside the domain.
        :param show_mesh: Plot the mesh.
        :return: The created GeoAxes instance.
        """
        if not self.is_read:
            self._read()

        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon
        from mpl_toolkits.basemap import Basemap

        if ax is None:
            ax = plt.gca()
        plt.subplots_adjust(left=0.05, right=0.95)

        # if global mesh return moll
        if self.is_global_mesh:
            m = Basemap(projection='moll', lon_0=0, resolution="c", ax=ax)
            _plot_features(m, stepsize=45.0)
            return m

        lat_extent = self.max_lat - self.min_lat
        lon_extent = self.max_lon - self.min_lon
        max_extent = max(lat_extent, lon_extent)

        # Use a global plot for very large domains.
        if lat_extent >= 120.0 and lon_extent >= 120.0:
            m = Basemap(projection='ortho', lon_0=20.,
                        lat_0=3.6, resolution="c", ax=ax)
            stepsize = 45.0

        elif max_extent >= 75.0:
            m = Basemap(projection='ortho', lon_0=self.center_lon,
                        lat_0=self.center_lat, resolution="c", ax=ax)
            stepsize = 10.0
        else:
            resolution = "l"

            # Calculate approximate width and height in meters.
            width = lon_extent
            height = lat_extent

            if width > 50.0:
                stepsize = 10.0
            elif 20.0 < width <= 50.0:
                stepsize = 5.0
            elif 5.0 < width <= 20.0:
                stepsize = 2.0
            else:
                stepsize = 1.0

            width *= 110000 * 1.1
            height *= 110000 * 1.3

            m = Basemap(projection='lcc', resolution=resolution, width=width,
                        height=height, lat_0=self.center_lat,
                        lon_0=self.center_lon, ax=ax)

        try:
            sorted_indices = self.get_sorted_edge_coords()
            x, y, z = self.domain_edge_coords[np.append(sorted_indices, 0)].T
            lats, lons, _ = xyz_to_lat_lon_radius(x, y, z)
            lines = np.array([lats, lons]).T
            #_plot_lines(m, lines, color="black", lw=2, label="Domain Edge")

            if False:# plot_inner_boundary:
                # Get surface points
                x, y, z = self.earth_surface_coords.T
                latlonrad = np.array(xyz_to_lat_lon_radius(x, y, z))

                # This part is potentially slow when lots
                # of points need to be checked
                in_domain = []
                idx = 0
                for lat, lon, _ in latlonrad.T:
                    if self.point_in_domain(latitude=lat, longitude=lon):
                        in_domain.append(idx)
                    idx += 1
                lats, lons, rad = np.array(latlonrad[:, in_domain])

                # Get the complex hull from projected (to 2D) points
                from scipy.spatial import ConvexHull
                x, y = m(lons, lats)
                points = np.array((x, y)).T
                hull = ConvexHull(points)

                # Plot the hull simplices
                for simplex in hull.simplices:
                    m.plot(points[simplex, 0], points[simplex, 1], color="0.5",
                           zorder=6)

        except LASIFError:
            # Back up plot if the other one fails, which happens for
            # very weird meshes sometimes.
            # This Scatter all edge nodes on the plotted domain
            x, y, z = self.domain_edge_coords.T
            lats, lons, _ = xyz_to_lat_lon_radius(x, y, z)
            x, y = m(lons, lats)
            m.scatter(x, y, color='k', label="Edge nodes", zorder=3000)

        if show_mesh:
            with exodus(self.exodus_file, mode='r') as e:
                if "r1" not in e.get_side_set_names():
                    msg = "Mesh not plotted as side set `r1` not part of mesh"
                    warnings.warn(msg, LASIFWarning)
                else:
                    num_nodes, node_ids = e.get_side_set_node_list(
                        e.get_side_set_ids()[
                            e.get_side_set_names().index("r1")])
                    # SHould not really happen - maybe with tet meshes?
                    if not np.array_equal(np.unique(num_nodes), [4]):  # NOQA
                        raise NotImplementedError
                    # A bit ugly here that we read all points but probably
                    # still faster than doing it directly on HDF5 with all
                    # kinds or reordering tricks.
                    points = np.array(e.get_coords()).T[node_ids - 1]
                    lats, lons, _ = xyz_to_lat_lon_radius(
                        points[:, 0], points[:, 1], points[:, 2])
                    x, y = m(lons, lats)
                    x = x.reshape((len(x) // 4, 4))
                    y = y.reshape((len(y) // 4, 4))
                    polygons = [
                        Polygon(np.array([_x, _y]).T,
                                facecolor=(0.90, 0.55, 0.28, 0.5),
                                edgecolor=(0.1, 0.1, 0.1, 0.5),
                                zorder=5, linewidth=0.5)
                        for _x, _y in zip(x, y)]
                    for p in polygons:
                        m.ax.add_patch(p)

        _plot_features(m, stepsize=stepsize)
        #ax.legend(framealpha=0.5, loc="lower right")
        return m

    def get_sorted_edge_coords(self):
        """
        Gets the indices of a sorted array of domain edge nodes,
        this method should work, as long as the top surfaces of the elements
        are approximately square
        """

        if not self.KDTrees_initialized:
            self._initialize_kd_trees()

        # For each point get the indices of the five nearest points, of
        # which the first one is the point itself.
        _, indices_nearest = self.domain_edge_tree.query(
            self.domain_edge_coords, k=5)

        num_edge_points = len(self.domain_edge_coords)
        indices_sorted = np.zeros(num_edge_points, dtype=int)

        # start sorting with the first node
        indices_sorted[0] = 0
        for i in range(num_edge_points)[1:]:
            prev_idx = indices_sorted[i - 1]
            # take 4 closest points
            closest_indices = indices_nearest[prev_idx, 1:]
            if not closest_indices[0] in indices_sorted:
                indices_sorted[i] = closest_indices[0]
            elif not closest_indices[1] in indices_sorted:
                indices_sorted[i] = closest_indices[1]
            elif not closest_indices[2] in indices_sorted:
                indices_sorted[i] = closest_indices[2]
            elif not closest_indices[3] in indices_sorted:
                indices_sorted[i] = closest_indices[3]
            else:
                raise LASIFError("Edge node sort algorithm only works "
                                 "for reasonably square elements")
        return indices_sorted

    def get_max_extent(self):
        """
        Returns the maximum extends of the domain.

        Returns a dictionary with the following keys:
            * minimum_latitude
            * maximum_latitude
            * minimum_longitude
            * maximum_longitude
        """
        if not self.is_read:
            self._read()

        if self.is_global_mesh:
            return {"minimum_latitude": -90.0,
                    "maximum_latitude": 90.0,
                    "minimum_longitude": -180.0,
                    "maximum_longitude": 180.0}

        return {"minimum_latitude": self.min_lat,
                "maximum_latitude": self.max_lat,
                "minimum_longitude": self.min_lon,
                "maximum_longitude": self.max_lon}

    def __str__(self):
        return "Exodus Domain"

    def is_global_domain(self):
        if not self.is_read:
            self._read()

        if self.is_global_mesh:
            return True
        return False


def _plot_features(map_object, stepsize):
    """
    Helper function aiding in consistent plot styling.
    """
    import matplotlib.pyplot as plt

    map_object.drawmapboundary(fill_color='#bbbbbb')
    map_object.fillcontinents(color='white', lake_color='#cccccc', zorder=1)
    plt.gcf().patch.set_alpha(0.0)

    # Style for parallels and meridians.
    LINESTYLE = {
        "linewidth": 0.5,
        "dashes": [],
        "color": "#999999"}

    # Parallels.
    if map_object.projection in ["moll", "laea"]:
        label = True
    else:
        label = False
    parallels = np.arange(-90.0, 90.0, stepsize)
    map_object.drawparallels(parallels, labels=[False, label, False, False],
                             zorder=200, **LINESTYLE)
    # Meridians.
    if map_object.projection in ["laea"]:
        label = True
    else:
        label = False
    meridians = np.arange(0.0, 360.0, stepsize)
    map_object.drawmeridians(
        meridians, labels=[False, False, False, label], zorder=200,
        **LINESTYLE)

    map_object.drawcoastlines(color="#444444", linewidth=0.7)
    map_object.drawcountries(linewidth=0.2, color="#999999")


def _plot_lines(map_object, lines, color, lw, alpha=1.0, label=None,
                effects=False):
    import matplotlib.patheffects as PathEffects

    lines = np.array(lines)
    lats = lines[:, 0]
    lngs = lines[:, 1]
    lngs, lats = map_object(lngs, lats)

    # Fix to avoid deal with basemaps inability to plot lines across map
    # boundaries.
    # XXX: No local area stitching so far!
    if map_object.projection == "ortho":
        lats = np.ma.masked_greater(lats, 1E15)
        lngs = np.ma.masked_greater(lngs, 1E15)
    elif map_object.projection == "moll":
        x = np.diff(lngs)
        y = np.diff(lats)
        lats = np.ma.array(lats, mask=False)
        lngs = np.ma.array(lngs, mask=False)
        max_jump = 0.3 * min(
            map_object.xmax - map_object.xmin,
            map_object.ymax - map_object.ymin)
        idx_1 = np.where(np.abs(x) > max_jump)
        idx_2 = np.where(np.abs(y) > max_jump)
        if idx_1:
            lats.mask[idx_1] = True
        if idx_2:
            lats.mask[idx_2] = True
        lngs.mask = lats.mask

    path_effects = [PathEffects.withStroke(linewidth=5, foreground="white")] \
        if effects else None

    map_object.plot(lngs, lats, color=color, lw=lw, alpha=alpha,
                    label=label, path_effects=path_effects)
