
#--------- ref-DAVIS -------

save_path=output/rvos/ref-DAVIS

deepspeed instructseg/eval/seg/eval_rvos.py \
    --model_path model/InstructSeg \
    --json_path dataset/rvos/DAVIS/valid/refdavis_valid.json \
    --image_folder dataset/rvos/DAVIS/valid/JPEGImages \
    --save_path ${save_path} \
    --use_soft True \
    --dataset_name 'RefDAVIS' \
    --use_temporal_query True \
    --use_vmtf True \

cd instructseg/eval/eval_tools/davis2017-evaluation
python evaluation_method.py --task unsupervised --results_path ${save_path}/inference/ReferDAVIS --davis_path dataset/rvos/DAVIS



#------ RefYoutube ------
save_path=output/rvos/ref-Youtube

deepspeed instructseg/eval/seg/eval_rvos.py \
    --model_path model/InstructSeg \
    --json_path dataset/rvos/YouTube/valid/refyoutube_valid.json \
    --image_folder dataset/rvos/YouTube/valid/JPEGImages \
    --save_path ${save_path} \
    --use_soft False \
    --dataset_name 'RefYoutube' \
    --use_temporal_query True \
    --use_vmtf True \


cd ${save_path}
zip -r instructseg-refyoutube.zip Annotations

