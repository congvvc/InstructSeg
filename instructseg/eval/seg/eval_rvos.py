import torch
import torch.nn as nn
import torch.distributed as distributed
import os
from enum import Enum
from tqdm import tqdm
import numpy as np


from instructseg.utils import conversation as conversation_lib
from instructseg.utils.builder import load_pretrained_model
from instructseg.datasets.InstructSegDatasets import RefDAVIS_Dataset, DataCollatorForCOCODatasetV2


from PIL import Image
from transformers import SiglipImageProcessor

from detectron2.data import MetadataCatalog, DatasetCatalog
from typing import Dict, Optional, Sequence, List
from dataclasses import dataclass, field
import transformers
from pathlib import Path


def init_distributed_mode(para):
    para.distributed = True
    if torch.cuda.device_count() <= 1:
        para.distributed = False
        para.local_rank = 0
        para.world_size = 1

    if para.distributed:
         # Init distributed environment
        distributed.init_process_group(backend="nccl")

        local_rank = distributed.get_rank()
        world_size = distributed.get_world_size()
        torch.cuda.set_device(local_rank)
        print('I am rank %d in this world of size %d!' % (local_rank, world_size))
        para.local_rank = local_rank
        para.world_size = world_size


@dataclass
class DataArguments:

    local_rank: int = 0

    lora_enable: bool = True
    lora_r: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    lora_weight_path: str = ""
    lora_bias: str = "none"  

    vision_tower: str = "/pretrained_model/CLIP/siglip-so400m-patch14-384"
    vision_tower_mask: str = "/pretrained_model/mask2former/maskformer2_swin_base_IN21k_384_bs16_50ep.pkl"


    lazy_preprocess: bool = False
    is_multimodal: bool = False
    image_folder: Optional[str] = field(default='dataset/rvos/ref-davis/DAVIS/JPEGImages/480p')
    model_path: Optional[str] = field(default="model/InstructSeg")
    mask_config: Optional[str] = field(default="instructseg/model/mask_decoder/mask_config/maskformer2_swin_base_384_bs16_50ep.yaml")
    image_aspect_ratio: str = 'square'
    image_grid_pinpoints: Optional[str] = field(default=None)

    json_path: str = 'dataset/rvos/ref-davis/refdavis_valid.json' 
    save_path: str = 'output/rvos/ref-DAVIS'
    model_map_name: str = 'instructseg'
    version: str = 'llava_phi'
    segmentation: bool = True
    eval_batch_size: int = 1
    dataloader_num_workers: int = 8
    seg_task: Optional[str] = field(default="referring")

    dataset_name: str = 'RefDAVIS' # RefDAVIS, RefYoutube
    use_soft: bool = True

    reference_frame_num: int =  4

    # enable OVP module in the paper
    use_temporal_query:  bool = True
    # enable VMTF module in the paper
    use_vmtf: bool = True




def fuse_davis_mask(mask_list,fill_number_list):
    fused_mask = np.zeros_like(mask_list[0])

    for mask, fill_number in zip(mask_list,fill_number_list):
        fused_mask[mask == 1] = fill_number
    return fused_mask


def get_reference_idx_inorder(cur_id, video_len, reference_frame_num):

    if reference_frame_num == 0:
        return []

    refer_list = []

    half_reference_frame_num = reference_frame_num // 2

    if cur_id < half_reference_frame_num:
        refer_list = [cur_id-k for k in range(1, cur_id+1)][::-1] + [cur_id+k for k in range(1, reference_frame_num-cur_id+1)]

    elif cur_id >= video_len-half_reference_frame_num:
        refer_list = [cur_id-k for k in range(1, reference_frame_num-video_len+cur_id+2)][::-1] + [cur_id+k for k in range(1, video_len-cur_id)]
    else:
        refer_list = [cur_id-k for k in range(1, half_reference_frame_num+1)][::-1] + [cur_id+k for k in range(1, half_reference_frame_num+1)]

    return refer_list


def inference_rvos_offline(model, batched_inputs, data_args, palette):
    height = batched_inputs['seg_info'][0]['height']
    width = batched_inputs['seg_info'][0]['width']
    video_length = len(batched_inputs['seg_info'][0]['file_name'])
    # during inference, batch size always 1 per gpu
    # for ref-davis, we inference the video per frame, and all objects should be in the same image.
    video_name = batched_inputs['seg_info'][0]["video"]
    file_names = batched_inputs['seg_info'][0]["file_name"]
    mask_file_names = [x.split("/")[-1].replace(".jpg", ".png") for x in file_names]

    # inference batch size = 1
    expressions = batched_inputs['seg_info'][0]["expressions"][0] # ["exp1", "exp2", ...]
    num_expressions = len(expressions) # i.e. num_object

    all_final_masks = [] # list[list[np.array]], first list: all_objects, second list: all_frames

    save_dir = os.path.join(data_args.save_path, "inference", 'ReferDAVIS')
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    # for each frame
    for frame_idx in range(video_length):

        # get reference images_clip for ovp module
        reference_idx = get_reference_idx_inorder(frame_idx, video_length, data_args.reference_frame_num)

        if len(reference_idx) == 0:
            images_clip=batched_inputs['images_clip'][0:1, frame_idx:frame_idx+1].float()
        else:
            images_clip=torch.cat([batched_inputs['images_clip'][0:1, frame_idx:frame_idx+1], batched_inputs['images_clip'][0:1, reference_idx]], dim=1).float()


        final_masks = []
        # images: [bs, video_length, c, h, w]
        # for each object
        for obj_id in range(num_expressions):
            output = model.eval_seg(
                input_ids=batched_inputs['input_ids'],
                attention_mask=batched_inputs['attention_mask'],
                images=batched_inputs['images'][0:1, frame_idx:frame_idx+1].float(),
                images_clip=images_clip,
                seg_info=batched_inputs['seg_info'],
                token_refer_id = [batched_inputs['token_refer_id'][obj_id]],
                refer_embedding_indices=batched_inputs['refer_embedding_indices'],
                labels=batched_inputs['labels'],
                use_soft = data_args.use_soft,
                padding_mask=batched_inputs['seg_info'][0]["padding_mask"][frame_idx],
                video_on = True,
            )
            pred_mask = output[0]['instances'].pred_masks
            # pred_mask = pred_mask.cpu().numpy()
            scores = output[0]['instances'].scores # .cpu().numpy()
            # pick mask with maximum score
            topk_scores,idx = torch.topk(scores,1)
            topk_pred_mask = pred_mask[idx,:].cpu().numpy()

            final_masks.append(topk_pred_mask[0])

            del output

        if data_args.use_soft:
            cur_masks = np.array(final_masks) # [N_obj, H, W]
            cur_obj_ids_int = [int(x) for x in range(num_expressions)]
            mask_merge = np.zeros((height, width, len(cur_obj_ids_int)+1)) # [H, W, N_obj+1], NOTE: mask_merge has additional background channel
            tmp_list = []
            for cur_id in cur_obj_ids_int:
                mask_merge[:, :, cur_id+1] = cur_masks[cur_id]
                tmp_list.append(cur_masks[cur_id])
            if len(tmp_list) != 0: # calculate the background prob
                back_prob = np.prod(1 - np.stack(tmp_list, axis=-1), axis=-1, keepdims=False)
                mask_merge[:, :, 0] = back_prob
            mask_merge = np.argmax(mask_merge, axis=-1).astype(np.uint8) # (H, W)
        
        else:
            fill_number_list = [int(x)+1 for x in range(num_expressions)]
            mask_merge = fuse_davis_mask(mask_list=final_masks, fill_number_list=fill_number_list).astype(np.uint8)
            
        mask_merge_final = Image.fromarray(mask_merge).convert('P')
        mask_merge_final.putpalette(palette)
        save_img_dir = os.path.join(save_dir, video_name)
        os.makedirs(save_img_dir, exist_ok=True)
        mask_merge_final.save(os.path.join(save_img_dir, mask_file_names[frame_idx]))

    return




def inference_rvos_youtube(model, batched_inputs, data_args):
    height = batched_inputs['seg_info'][0]['height']
    width = batched_inputs['seg_info'][0]['width']
    video_length = len(batched_inputs['seg_info'][0]['file_name'])
    # during inference, batch size always 1 per gpu
    # for ref-davis, we inference the video per frame, and all objects should be in the same image.
    video_name = batched_inputs['seg_info'][0]["video"]
    file_names = batched_inputs['seg_info'][0]["file_name"]
    exp_id = batched_inputs['seg_info'][0]["exp_id"]
    mask_file_names = [x.split("/")[-1].replace(".jpg", ".png") for x in file_names]

    # inference batch size = 1
    expressions = batched_inputs['seg_info'][0]["expressions"] # ["exp1", "exp2", ...]
    num_expressions = 1 # 1 object for ref-youtubevos


    save_dir = os.path.join(data_args.save_path, 'Annotations')
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    # for each frame
    for frame_idx in range(video_length):

        # get reference images_clip for ovp module
        reference_idx = get_reference_idx_inorder(frame_idx, video_length, data_args.reference_frame_num)

        if len(reference_idx) == 0:
            images_clip=batched_inputs['images_clip'][0:1, frame_idx:frame_idx+1].float()
        else:
            images_clip=torch.cat([batched_inputs['images_clip'][0:1, frame_idx:frame_idx+1], batched_inputs['images_clip'][0:1, reference_idx]], dim=1).float()



        # get final masks of a expression, 
        # list[np.array], length is video_len, array size of [H, W], indicates the mask logits
        final_masks = []
        # images: [bs, video_length, c, h, w]
        # for each object
        for obj_id in range(num_expressions):
            output = model.eval_seg(
                input_ids=batched_inputs['input_ids'],
                attention_mask=batched_inputs['attention_mask'],
                images=batched_inputs['images'][0:1, frame_idx:frame_idx+1].float(),
                images_clip=images_clip,
                seg_info=batched_inputs['seg_info'],
                token_refer_id = [batched_inputs['token_refer_id'][obj_id]],
                refer_embedding_indices=batched_inputs['refer_embedding_indices'],
                labels=batched_inputs['labels'],
                use_soft = False,
                padding_mask=batched_inputs['seg_info'][0]["padding_mask"][frame_idx],
                video_on = True,
            )
            pred_mask = output[0]['instances'].pred_masks
            # pred_mask = pred_mask.cpu().numpy()
            scores = output[0]['instances'].scores # .cpu().numpy()
            # pick mask with maximum score
            topk_scores,idx = torch.topk(scores,1)
            topk_pred_mask = pred_mask[idx,:].cpu().numpy()

            final_masks.append(topk_pred_mask[0])

            del output

        cur_masks = final_masks[0] # [H, W]

        mask = cur_masks.astype(np.float32) 
        mask = Image.fromarray(mask * 255).convert('L')

        save_img_dir = os.path.join(save_dir, video_name, exp_id)
        os.makedirs(save_img_dir, exist_ok=True)
        mask.save(os.path.join(save_img_dir, mask_file_names[frame_idx]))

    return






def evaluation(data_args):
    
    init_distributed_mode(data_args)

    model_path = os.path.expanduser(data_args.model_path)

    tokenizer, model, image_processor, context_len = load_pretrained_model(model_path, model_args=data_args, mask_config=data_args.mask_config, device='cuda')

    device = torch.device(data_args.local_rank if torch.cuda.is_available() else "cpu") 
    model.to(dtype=torch.float32, device=device)


    data_args.image_processor = image_processor
    data_args.is_multimodal = True
    conversation_lib.default_conversation = conversation_lib.conv_templates[data_args.version]

    clip_image_processor = SiglipImageProcessor.from_pretrained(data_args.vision_tower)
    eval_dataset = RefDAVIS_Dataset(json_path=data_args.json_path, image_path_yv=data_args.image_folder, tokenizer=tokenizer, data_args=data_args, clip_image_processor=clip_image_processor, is_train=False)
    data_collator = DataCollatorForCOCODatasetV2(tokenizer=tokenizer, clip_image_processor=clip_image_processor)

    dataloader_params = {
        "batch_size": data_args.eval_batch_size,
        "num_workers": data_args.dataloader_num_workers,
    }


    if data_args.distributed:
        val_sampler = torch.utils.data.distributed.DistributedSampler(eval_dataset, shuffle=False, drop_last=False)
    else:
        val_sampler = None
    eval_dataloader = torch.utils.data.DataLoader(
        eval_dataset,
        batch_size=dataloader_params['batch_size'],
        shuffle=False,
        num_workers=dataloader_params['num_workers'],
        pin_memory=False,
        sampler=val_sampler,
        collate_fn=data_collator)


    def load_ref_dataset():
        return RefDAVIS_Dataset(json_path=data_args.json_path, image_path_yv=data_args.image_folder, tokenizer=tokenizer, data_args=data_args, clip_image_processor=clip_image_processor)

    DatasetCatalog.register('rvos_dataset', load_ref_dataset)
    MetadataCatalog.get('rvos_dataset').set(stuff_classes=['object'],)


    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    # model.to(device=device,dtype=torch.float).eval()

    model.eval()

    if data_args.dataset_name == "RefDAVIS":
        palette_img = "dataset/rvos/ref-davis/DAVIS/trainval/Annotations/Full-Resolution/bear/00000.png"
        palette = Image.open(palette_img).getpalette()

    with torch.no_grad():
        for idx, inputs in tqdm(enumerate(eval_dataloader), total=len(eval_dataloader)):
            video_name = inputs['seg_info'][0]['file_name'][0].split('/')[-2]


            inputs = {k: v.to(device) if torch.is_tensor(v) else v for k, v in inputs.items()}
            inputs['token_refer_id'] = [ids.to(device) for ids in inputs['token_refer_id'][0]]
            if data_args.dataset_name=='RefDAVIS': # RefDAVIS, RefYoutube
                inference_rvos_offline(model, inputs, data_args, palette)
            elif data_args.dataset_name=='RefYoutube':
                inference_rvos_youtube(model, inputs, data_args)
   
            if torch.cuda.is_available():
                torch.cuda.synchronize()

            print(f'=====>finish eval {video_name}')

if __name__ == '__main__':

    parser = transformers.HfArgumentParser(DataArguments)
    data_args = parser.parse_args_into_dataclasses()[0]

    evaluation(data_args)