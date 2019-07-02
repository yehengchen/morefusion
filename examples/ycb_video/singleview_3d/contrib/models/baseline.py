import chainer
from chainer.backends import cuda
import chainer.functions as F
import chainer.links as L
from chainercv.links.model.vgg import VGG16
import numpy as np

import objslampp


class BaselineModel(chainer.Chain):

    _models = objslampp.datasets.YCBVideoModels()

    def __init__(
        self,
        *,
        n_fg_class,
        freeze_until,
        voxelization,
        use_occupancy=False,
    ):
        super().__init__()

        self._n_fg_class = n_fg_class
        self._freeze_until = freeze_until
        self._voxelization = voxelization
        self._use_occupancy = use_occupancy

        kwargs = {'initialW': chainer.initializers.Normal(0.01)}
        with self.init_scope():
            self.extractor = VGG16(pretrained_model='imagenet')
            self.extractor.pick = ['conv4_3', 'conv3_3', 'conv2_2', 'conv1_2']
            self.extractor.remove_unused()
            self.conv5 = L.Convolution2D(
                in_channels=512,
                out_channels=16,
                ksize=1,
                **kwargs,
            )

            # voxelization_3d -> (16, 32, 32, 32)
            self._voxel_dim = 32

            if self._use_occupancy:
                # target occupied (16)
                # nontarget occupied (1)
                # empty (1)
                in_channels = 16 + 2
            else:
                in_channels = 16

            self.conv6 = L.Convolution3D(
                in_channels=in_channels,
                out_channels=16,
                ksize=4,
                stride=2,
                pad=1,
                **kwargs,
            )  # 32x32x32 -> 16x16x16
            self.conv7 = L.Convolution3D(
                in_channels=16,
                out_channels=16,
                ksize=4,
                stride=2,
                pad=1,
                **kwargs,
            )  # 16x16x16 -> 8x8x8

            # 16 * 8 * 8 * 8 = 8192
            self.fc8 = L.Linear(8192, 1024, **kwargs)
            self.fc_quaternion = L.Linear(1024, 4 * n_fg_class, **kwargs)
            self.fc_translation = L.Linear(1024, 3 * n_fg_class, **kwargs)

    def predict(
        self,
        *,
        class_id,
        pitch,
        origin,
        rgb,
        pcd,
        grid_nontarget=None,
        grid_empty=None,
    ):
        xp = self.xp

        B, H, W, C = rgb.shape
        assert H == W == 256
        assert C == 3
        dimensions = (self._voxel_dim,) * 3

        # prepare
        pitch = pitch.astype(np.float32)
        rgb = rgb.transpose(0, 3, 1, 2).astype(np.float32)  # BHWC -> BCHW
        pcd = pcd.transpose(0, 3, 1, 2).astype(np.float32)  # BHW3 -> B3HW
        if self._use_occupancy:
            # BXYZ -> B1XYZ
            assert grid_empty.shape[1:] == dimensions
            assert grid_nontarget.shape[1:] == dimensions
            grid_nontarget = grid_nontarget[:, None, :, :, :].astype(
                np.float32
            )
            grid_empty = grid_empty[:, None, :, :, :].astype(np.float32)

        # feature extraction
        mean = xp.asarray(self.extractor.mean)
        h_conv4_3, h_conv3_3, h_conv2_2, h_conv1_2 = self.extractor(
            rgb - mean[None]
        )
        if self._freeze_until == 'conv4_3':
            h_conv4_3.unchain_backward()
        elif self._freeze_until == 'conv3_3':
            h_conv3_3.unchain_backward()
        elif self._freeze_until == 'conv2_2':
            h_conv2_2.unchain_backward()
        elif self._freeze_until == 'conv1_2':
            h_conv1_2.unchain_backward()
        else:
            self._freeze_until == 'none'
        del h_conv3_3, h_conv2_2, h_conv1_2
        h = h_conv4_3  # 1/8   # 256x256 -> 32x32
        h = F.relu(self.conv5(h))
        h = F.resize_images(h, (H, W))

        h_vox = []
        for i in range(B):
            h_i = h[i]
            pcd_i = pcd[i]

            h_i = h_i.transpose(1, 2, 0)      # CHW -> HWC
            pcd_i = pcd_i.transpose(1, 2, 0)  # 3HW -> HW3
            mask_i = ~xp.isnan(pcd_i).any(axis=2)

            values = h_i[mask_i, :]    # MC
            points = pcd_i[mask_i, :]  # M3

            if self._voxelization == 'average':
                func = objslampp.functions.average_voxelization_3d
            else:
                assert self._voxelization == 'max'
                func = objslampp.functions.max_voxelization_3d
            h_i = func(
                values=values,
                points=points,
                origin=origin[i],
                pitch=pitch[i],
                dimensions=dimensions,
                channels=h_i.shape[2],
            )  # CXYZ
            h_vox.append(h_i[None])
        h = F.concat(h_vox, axis=0)          # BCXYZ
        del h_vox

        if self._use_occupancy:
            # BCXYZ + B1XYZ + B1XYZ -> B(C+2)XYZ
            h = F.concat([h, grid_nontarget, grid_empty], axis=1)

        h = F.relu(self.conv6(h))
        h = F.relu(self.conv7(h))
        h = F.relu(self.fc8(h))

        quaternion = self.fc_quaternion(h)
        quaternion = quaternion.reshape(B, self._n_fg_class, 4)
        translation = self.fc_translation(h)
        translation = translation.reshape(B, self._n_fg_class, 3)

        fg_class_id = class_id - 1
        quaternion = quaternion[xp.arange(B), fg_class_id, :]
        translation = translation[xp.arange(B), fg_class_id, :]

        quaternion = F.normalize(quaternion, axis=1)
        translation = origin + translation * pitch[:, None] * self._voxel_dim

        return quaternion, translation

    def __call__(
        self,
        *,
        class_id,
        pitch,
        origin,
        rgb,
        pcd,
        quaternion_true,
        translation_true,
        grid_target=None,
        grid_nontarget=None,
        grid_empty=None,
    ):
        del grid_target  # unused

        keep = class_id != -1
        if keep.sum() == 0:
            return chainer.Variable(self.xp.zeros((), dtype=np.float32))

        class_id = class_id[keep]
        pitch = pitch[keep]
        origin = origin[keep]
        rgb = rgb[keep]
        pcd = pcd[keep]
        quaternion_true = quaternion_true[keep]
        translation_true = translation_true[keep]
        if self._use_occupancy:
            assert grid_nontarget is not None
            assert grid_empty is not None
            grid_nontarget = grid_nontarget[keep]
            grid_empty = grid_empty[keep]

        quaternion_pred, translation_pred = self.predict(
            class_id=class_id,
            pitch=pitch,
            origin=origin,
            rgb=rgb,
            pcd=pcd,
            grid_nontarget=grid_nontarget,
            grid_empty=grid_empty,
        )

        self.evaluate(
            class_id=class_id,
            quaternion_true=quaternion_true,
            translation_true=translation_true,
            quaternion_pred=quaternion_pred,
            translation_pred=translation_pred,
        )

        loss = self.loss(
            class_id=class_id,
            quaternion_true=quaternion_true,
            translation_true=translation_true,
            quaternion_pred=quaternion_pred,
            translation_pred=translation_pred,
        )
        return loss

    def evaluate(
        self,
        *,
        class_id,
        quaternion_true,
        translation_true,
        quaternion_pred,
        translation_pred,
    ):
        batch_size = class_id.shape[0]

        T_cad2cam_true = objslampp.functions.quaternion_matrix(quaternion_true)
        T_cad2cam_pred = objslampp.functions.quaternion_matrix(quaternion_pred)
        T_cad2cam_true = objslampp.functions.compose_transform(
            Rs=T_cad2cam_true[:, :3, :3], ts=translation_true,
        )
        T_cad2cam_pred = objslampp.functions.compose_transform(
            Rs=T_cad2cam_pred[:, :3, :3], ts=translation_pred,
        )
        T_cad2cam_true = cuda.to_cpu(T_cad2cam_true.array)
        T_cad2cam_pred = cuda.to_cpu(T_cad2cam_pred.array)

        summary = chainer.DictSummary()
        for i in range(batch_size):
            class_id_i = int(class_id[i])
            cad_pcd = self._models.get_pcd(class_id=class_id_i)
            add, add_s = objslampp.metrics.average_distance(
                [cad_pcd], [T_cad2cam_true[i]], [T_cad2cam_pred[i]]
            )
            add, add_s = add[0], add_s[0]
            if chainer.config.train:
                summary.add({'add': add, 'add_s': add_s})
            else:
                summary.add({
                    f'add/{class_id_i:04d}': add,
                    f'add_s/{class_id_i:04d}': add_s,
                })
        chainer.report(summary.compute_mean(), self)

    def loss(
        self,
        *,
        class_id,
        quaternion_true,
        translation_true,
        quaternion_pred,
        translation_pred,
    ):
        T_cad2cam_true = objslampp.functions.quaternion_matrix(quaternion_true)
        T_cad2cam_pred = objslampp.functions.quaternion_matrix(quaternion_pred)

        T_cad2cam_true = objslampp.functions.compose_transform(
            T_cad2cam_true[:, :3, :3], translation_true,
        )
        T_cad2cam_pred = objslampp.functions.compose_transform(
            T_cad2cam_pred[:, :3, :3], translation_pred,
        )

        batch_size = class_id.shape[0]

        loss = 0
        for i in range(batch_size):
            cad_pcd = self._models.get_pcd(class_id=int(class_id[i]))
            cad_pcd = self.xp.asarray(cad_pcd)
            loss_i = objslampp.functions.average_distance_l1(
                cad_pcd,
                T_cad2cam_true[i:i + 1],
                T_cad2cam_pred[i:i + 1],
            )[0]
            loss += loss_i
        loss /= batch_size

        values = {'loss': loss}
        chainer.report(values, observer=self)

        return loss
