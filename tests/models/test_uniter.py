# Copyright (c) Facebook, Inc. and its affiliates.
import gc
import unittest

import torch
from mmf.common.sample import SampleList
from mmf.models.uniter import (
    UniterForClassification,
    UniterForPretraining,
    UniterImageEmbeddings,
    UniterModelBase,
)
from mmf.utils.general import get_current_device
from omegaconf import OmegaConf


class TestUniterImageEmbeddings(unittest.TestCase):
    def test_forward_has_correct_output_dim(self):
        bs = 32
        num_feat = 100
        config = OmegaConf.create({"img_dim": 1024, "hidden_size": 256, "pos_dim": 7})
        embedding = UniterImageEmbeddings(config)
        img_feat = torch.rand((bs, num_feat, config["img_dim"]))
        img_pos_feat = torch.rand((bs, num_feat, config["pos_dim"]))
        type_embeddings = torch.ones((bs, num_feat, 1), dtype=torch.long)

        output = embedding(img_feat, img_pos_feat, type_embeddings, img_masks=None)
        self.assertEquals(list(output.shape), [32, 100, 256])


class TestUniterModelBase(unittest.TestCase):
    def tearDown(self):
        del self.model
        gc.collect()

    def test_pretrained_model(self):
        img_dim = 1024
        config = OmegaConf.create({"image_embeddings": {"img_dim": img_dim}})
        self.model = UniterModelBase(config)

        self.model.eval()
        self.model = self.model.to(get_current_device())

        bs = 8
        num_feats = 100
        max_sentence_len = 25
        pos_dim = 7
        input_ids = torch.ones((bs, max_sentence_len), dtype=torch.long)
        img_feat = torch.rand((bs, num_feats, img_dim))
        img_pos_feat = torch.rand((bs, num_feats, pos_dim))
        position_ids = torch.arange(
            0, input_ids.size(1), dtype=torch.long, device=img_feat.device
        ).unsqueeze(0)
        attention_mask = torch.ones((bs, max_sentence_len + num_feats))

        with torch.no_grad():
            model_output = self.model(
                input_ids, position_ids, img_feat, img_pos_feat, attention_mask
            )

        self.assertEqual(model_output.shape, torch.Size([8, 125, 768]))


class TestUniterWithHeads(unittest.TestCase):
    def tearDown(self):
        del self.model
        gc.collect()

    def _get_sample_list(self):
        bs = 8
        num_feats = 100
        max_sentence_len = 25
        img_dim = 2048
        cls_dim = 3129
        input_ids = torch.ones((bs, max_sentence_len), dtype=torch.long)
        input_mask = torch.ones((bs, max_sentence_len), dtype=torch.long)
        image_feat = torch.rand((bs, num_feats, img_dim))
        position_ids = torch.arange(
            0, max_sentence_len, dtype=torch.long, device=image_feat.device
        ).unsqueeze(0)
        img_pos_feat = torch.rand((bs, num_feats, 7))
        attention_mask = torch.zeros(
            (bs, max_sentence_len + num_feats), dtype=torch.long
        )
        image_mask = torch.zeros((bs, num_feats), dtype=torch.long)
        targets = torch.rand((bs, cls_dim))

        sample_list = SampleList()
        sample_list.add_field("input_ids", input_ids)
        sample_list.add_field("input_mask", input_mask)
        sample_list.add_field("image_feat", image_feat)
        sample_list.add_field("img_pos_feat", img_pos_feat)
        sample_list.add_field("attention_mask", attention_mask)
        sample_list.add_field("image_mask", image_mask)
        sample_list.add_field("targets", targets)
        sample_list.add_field("dataset_name", "test")
        sample_list.add_field("dataset_type", "test")
        sample_list["position_ids"] = position_ids

        return sample_list

    def test_uniter_for_classification(self):
        config = OmegaConf.create(
            {
                "heads": {"test": {"type": "mlp", "num_labels": 3129}},
                "tasks": "test",
                "losses": {"test": "logit_bce"},
            }
        )
        self.model = UniterForClassification(config)

        self.model.eval()
        self.model = self.model.to(get_current_device())
        sample_list = self._get_sample_list()

        with torch.no_grad():
            model_output = self.model(sample_list)

        self.assertTrue("losses" in model_output)
        self.assertTrue("test/test/logit_bce" in model_output["losses"])

    def _enhance_sample_list_for_pretraining(self, sample_list):
        bs = sample_list["input_ids"].size(0)
        sentence_len = sample_list["input_ids"].size(1)

        is_correct = torch.ones((bs,), dtype=torch.long)
        lm_label_ids = torch.zeros((bs, sentence_len), dtype=torch.long)
        input_ids_masked = sample_list["input_ids"]
        num_feat = sample_list["image_feat"].size(1)
        cls_dim = 1601
        image_info = {"cls_prob": torch.rand((bs, num_feat, cls_dim))}
        sample_list.add_field("is_correct", is_correct)
        sample_list.add_field("task", "mlm")
        sample_list.add_field("lm_label_ids", lm_label_ids)
        sample_list.add_field("input_ids_masked", input_ids_masked)
        sample_list.add_field("image_info_0", image_info)

    def test_uniter_for_pretraining(self):
        # UNITER pretraining has 5 pretraining tasks,
        # we have one unique head for each, and in each
        # forward pass we train on a different task.
        # In this test we try running a forward pass
        # through each head.
        config = OmegaConf.create(
            {
                "heads": {
                    "mlm": {"type": "mlm"},
                    "itm": {"type": "itm"},
                    "mrc": {"type": "mrc"},
                    "mrfr": {"type": "mrfr"},
                    "wra": {"type": "wra"},
                },
                "tasks": "mlm,itm,mrc,mrfr,wra",
                "mask_probability": 0.15,
            }
        )

        self.model = UniterForPretraining(config)
        self.model.eval()
        self.model = self.model.to(get_current_device())
        sample_list = self._get_sample_list()
        self._enhance_sample_list_for_pretraining(sample_list)

        expected_loss_names = {
            "mlm": "masked_lm_loss",
            "itm": "itm_loss",
            "mrc": "mrc_loss",
            "mrfr": "mrfr_loss",
            "wra": "wra_loss",
        }

        for task_name, loss_name in expected_loss_names.items():
            sample_list["task"] = task_name
            with torch.no_grad():
                model_output = self.model(sample_list)

            print(task_name)
            print(model_output["losses"].keys())
            self.assertTrue("losses" in model_output)
            self.assertTrue(loss_name in model_output["losses"])