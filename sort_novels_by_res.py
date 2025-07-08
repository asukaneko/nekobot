import json

# 读取JSON文件
with open('novel_details2.json', 'r', encoding='utf-8') as f:
    novels = json.load(f)

# 按res值从小到大排序
sorted_novels = sorted(novels.items(), key=lambda x: int(x[1]['res']))

# 输出排序结果
for title, details in sorted_novels:
    print(f"{title}: res={details['res']}")

# 可选：将排序结果保存到新文件
with open('sorted_novels_by_res.json', 'w', encoding='utf-8') as f:
    json.dump(dict(sorted_novels), f, ensure_ascii=False, indent=2)

print("排序完成，结果已保存到sorted_novels_by_res.json")